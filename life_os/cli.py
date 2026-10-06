"""Command-line interface for LIFE OS."""
from __future__ import annotations
import argparse
import json
from datetime import date
from pathlib import Path

from .attention import acknowledge_attention, decide_approval, list_approvals, list_attention
from .architecture_gap import recent_proposals as recent_architecture_gaps
from .app import serve_app
from .capabilities import list_capabilities, probe_local_ai
from .commercial_mailbox import ingest_commercial_event, recent_events
from .config import default_config
from .control import serve_dashboard
from .db import connect, initialize
from .engineering import get_run as get_engineering_run, recent_runs as recent_engineering_runs, submit as submit_engineering
from .engineering_delivery import get_promotion as get_engineering_promotion, recent_promotions as recent_engineering_promotions, request_promotion as request_engineering_promotion
from .money import list_payment_evidence
from .queue import initialize_queue, recent_jobs, set_state
from .reconcile import list_resources
from .reality import awareness_snapshot, ingest_fact, register_source, run_reality_scan, set_source_enabled
from .service import LifeOS
from .store import get_profile, set_profile
from .sync import sync_status
from .worker import run_worker, worker_status
from .windows_control_agent import run as run_windows_control

def parser() -> argparse.ArgumentParser:
    cfg=default_config()
    p=argparse.ArgumentParser(prog="life-os")
    p.add_argument("--db",default=str(cfg.db))
    s=p.add_subparsers(dest="command",required=True)

    sp=s.add_parser("profile-set"); sp.add_argument("key"); sp.add_argument("value")
    s.add_parser("profile")
    sg=s.add_parser("goal-add"); sg.add_argument("title"); sg.add_argument("--priority",type=int,default=50)
    st=s.add_parser("task-add"); st.add_argument("title"); st.add_argument("--priority",type=int,default=50)
    st.add_argument("--minutes",type=int,default=30); st.add_argument("--due",type=date.fromisoformat); st.add_argument("--goal",type=int)
    sd=s.add_parser("today"); sd.add_argument("--minutes",type=int,default=480)
    s.add_parser("now")
    sf=s.add_parser("done"); sf.add_argument("task_id",type=int)

    sw=s.add_parser("worker"); sw.add_argument("--once",action="store_true"); sw.add_argument("--poll-seconds",type=float,default=3.0)
    sw.add_argument("--home",default=str(cfg.home)); sw.add_argument("--backups",default=str(cfg.backups))
    sw.add_argument("--log",default=str(cfg.home/"logs"/"worker.log"))
    ss=s.add_parser("worker-status"); ss.add_argument("--json",action="store_true"); ss.add_argument("--jobs",type=int,default=0)
    s.add_parser("worker-pause"); s.add_parser("worker-resume")
    s.add_parser("worker-emergency-stop"); s.add_parser("worker-emergency-clear")
    wc=s.add_parser("windows-control"); wc.add_argument("--home",default=str(cfg.home)); wc.add_argument("--repo-root",default=str(Path(__file__).resolve().parents[1])); wc.add_argument("--poll-seconds",type=float,default=5.0)

    es=s.add_parser("engineering-submit"); es.add_argument("goal"); es.add_argument("--acceptance",action="append",required=True); es.add_argument("--repo-root"); es.add_argument("--home",default=str(cfg.home)); es.add_argument("--priority",type=int,default=85)
    er=s.add_parser("engineering-runs"); er.add_argument("--limit",type=int,default=20)
    eg=s.add_parser("engineering-run"); eg.add_argument("run_id",type=int)
    ag=s.add_parser("architecture-gaps"); ag.add_argument("--limit",type=int,default=50)
    ep=s.add_parser("engineering-promote"); ep.add_argument("run_id",type=int)
    eps=s.add_parser("engineering-promotions"); eps.add_argument("--limit",type=int,default=20)
    epg=s.add_parser("engineering-promotion"); epg.add_argument("promotion_id",type=int)

    al=s.add_parser("attention-list"); al.add_argument("--all",action="store_true"); al.add_argument("--limit",type=int,default=50)
    aa=s.add_parser("attention-ack"); aa.add_argument("attention_id",type=int)
    apl=s.add_parser("approvals-list"); apl.add_argument("--state",default="pending"); apl.add_argument("--limit",type=int,default=50)
    apa=s.add_parser("approval-approve"); apa.add_argument("approval_id",type=int)
    apd=s.add_parser("approval-deny"); apd.add_argument("approval_id",type=int)

    s.add_parser("capabilities-list")
    s.add_parser("capabilities-probe")
    s.add_parser("sync-status")
    rec=s.add_parser("reconcile-list"); rec.add_argument("--status"); rec.add_argument("--limit",type=int,default=100)
    rev=s.add_parser("revenue-list"); rev.add_argument("--limit",type=int,default=50)
    ci=s.add_parser("commercial-ingest"); ci.add_argument("event_json")
    ce=s.add_parser("commercial-events"); ce.add_argument("--limit",type=int,default=50)
    s.add_parser("reality-status")
    s.add_parser("reality-scan")
    rsr=s.add_parser("reality-source-register"); rsr.add_argument("source_key"); rsr.add_argument("title"); rsr.add_argument("--kind",choices=("bridge","connector"),default="bridge"); rsr.add_argument("--authority",type=int,default=80); rsr.add_argument("--poll-seconds",type=int,default=300); rsr.add_argument("--freshness-seconds",type=int,default=900); rsr.add_argument("--disabled",action="store_true")
    rse=s.add_parser("reality-source-enable"); rse.add_argument("source_key"); rse.add_argument("--disable",action="store_true")
    ri=s.add_parser("reality-ingest"); ri.add_argument("source_key"); ri.add_argument("fact_key"); ri.add_argument("domain_key"); ri.add_argument("kind"); ri.add_argument("value_json"); ri.add_argument("--unit",default=""); ri.add_argument("--confidence",type=float,default=1.0); ri.add_argument("--observed-at")
    ctl=s.add_parser("control-server"); ctl.add_argument("--host",default="127.0.0.1"); ctl.add_argument("--port",type=int,default=8765)
    app=s.add_parser("app"); app.add_argument("--host",default="127.0.0.1"); app.add_argument("--port",type=int,default=8766); app.add_argument("--no-open",action="store_true")
    return p

def _print_json(value) -> None:
    print(json.dumps(value,indent=2,sort_keys=True,default=str))

def main(argv=None)->int:
    a=parser().parse_args(argv)
    c=connect(a.db); initialize(c)
    try:
        os=LifeOS(c)
        if a.command=="profile-set":
            set_profile(c,a.key,a.value); print("saved")
        elif a.command=="profile":
            for k,v in get_profile(c).items(): print(f"{k}: {v}")
        elif a.command=="goal-add":
            print(os.create_goal(a.title,a.priority))
        elif a.command=="task-add":
            print(os.create_task(a.title,a.priority,a.minutes,a.due,a.goal))
        elif a.command=="today":
            for i,x in enumerate(os.today(a.minutes),1): print(f"{i}. [{x.score}] {x.task.title} ({x.task.effort_minutes}m)")
        elif a.command=="now":
            x=os.now(); print("Nothing queued." if x is None else f"{x.task.title} ({x.task.effort_minutes}m, score {x.score})")
        elif a.command=="done":
            print("completed" if os.finish(a.task_id) else "task not found or already completed")
        elif a.command=="worker":
            return run_worker(c,home=Path(a.home),backups=Path(a.backups),log_path=Path(a.log),once=a.once,poll_seconds=a.poll_seconds)
        elif a.command=="worker-status":
            initialize_queue(c); status=worker_status(c)
            if a.jobs: status["jobs"]=recent_jobs(c,a.jobs)
            print(json.dumps(status,sort_keys=True) if a.json else json.dumps(status,indent=2,sort_keys=True))
        elif a.command=="worker-pause":
            set_state(c,"worker.paused","1"); print("paused")
        elif a.command=="worker-resume":
            set_state(c,"worker.paused","0"); print("resumed")
        elif a.command=="worker-emergency-stop":
            set_state(c,"worker.emergency_stop","1"); print("emergency stop engaged")
        elif a.command=="worker-emergency-clear":
            set_state(c,"worker.emergency_stop","0"); print("emergency stop cleared")
        elif a.command=="windows-control":
            return run_windows_control(c,home=Path(a.home),repo=Path(a.repo_root),poll_seconds=a.poll_seconds)
        elif a.command=="engineering-submit":
            _print_json(submit_engineering(c,goal=a.goal,acceptance=a.acceptance,home=Path(a.home),repo_root=a.repo_root,priority=a.priority))
        elif a.command=="engineering-runs":
            _print_json(recent_engineering_runs(c,a.limit))
        elif a.command=="engineering-run":
            value=get_engineering_run(c,a.run_id); _print_json(value if value is not None else {"error":"not found","run_id":a.run_id})
        elif a.command=="architecture-gaps":
            _print_json(recent_architecture_gaps(c,a.limit))
        elif a.command=="engineering-promote":
            _print_json(request_engineering_promotion(c,run_id=a.run_id))
        elif a.command=="engineering-promotions":
            _print_json(recent_engineering_promotions(c,a.limit))
        elif a.command=="engineering-promotion":
            value=get_engineering_promotion(c,a.promotion_id); _print_json(value if value is not None else {"error":"not found","promotion_id":a.promotion_id})
        elif a.command=="attention-list":
            _print_json(list_attention(c,open_only=not a.all,limit=a.limit))
        elif a.command=="attention-ack":
            print("acknowledged" if acknowledge_attention(c,a.attention_id) else "not found or already acknowledged")
        elif a.command=="approvals-list":
            state=None if a.state=="all" else a.state; _print_json(list_approvals(c,state=state,limit=a.limit))
        elif a.command=="approval-approve":
            print("approved" if decide_approval(c,a.approval_id,"approved") else "not pending")
        elif a.command=="approval-deny":
            print("denied" if decide_approval(c,a.approval_id,"denied") else "not pending")
        elif a.command=="capabilities-list":
            _print_json(list_capabilities(c))
        elif a.command=="capabilities-probe":
            _print_json(probe_local_ai(c))
        elif a.command=="sync-status":
            _print_json(sync_status(c))
        elif a.command=="reconcile-list":
            _print_json(list_resources(c,status=a.status,limit=a.limit))
        elif a.command=="revenue-list":
            _print_json(list_payment_evidence(c,a.limit))
        elif a.command=="commercial-ingest":
            _print_json(ingest_commercial_event(c,json.loads(a.event_json)))
        elif a.command=="commercial-events":
            _print_json(recent_events(c,a.limit))
        elif a.command=="reality-status":
            _print_json(awareness_snapshot(c))
        elif a.command=="reality-scan":
            _print_json(run_reality_scan(c))
        elif a.command=="reality-source-register":
            register_source(
                c, source_key=a.source_key, title=a.title, kind=a.kind,
                authority=a.authority, poll_interval_seconds=a.poll_seconds,
                freshness_seconds=a.freshness_seconds, enabled=not a.disabled,
                health="unconfigured" if a.disabled else "unknown",
            ); print("registered")
        elif a.command=="reality-source-enable":
            print("updated" if set_source_enabled(c,a.source_key,not a.disable) else "source not found")
        elif a.command=="reality-ingest":
            _print_json(ingest_fact(
                c,source_key=a.source_key,fact_key=a.fact_key,domain_key=a.domain_key,
                kind=a.kind,value=json.loads(a.value_json),unit=a.unit,
                confidence=a.confidence,observed_at=a.observed_at,
            ))
        elif a.command=="control-server":
            c.close()
            serve_dashboard(a.db,host=a.host,port=a.port)
            return 0
        elif a.command=="app":
            c.close()
            serve_app(a.db,host=a.host,port=a.port,open_browser=not a.no_open)
            return 0
        return 0
    finally:
        try: c.close()
        except Exception: pass

if __name__=="__main__":
    raise SystemExit(main())
