"""Local-only control-plane snapshot and read-only dashboard."""
from __future__ import annotations
import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .attention import list_attention, list_approvals
from .capabilities import list_capabilities
from .db import connect, initialize
from .money import list_payment_evidence
from .queue import initialize_queue
from .reconcile import list_resources
from .sync import sync_status
from .worker import worker_status

def control_snapshot(connection:sqlite3.Connection)->dict:
    initialize_queue(connection)
    return {
        "worker":worker_status(connection),
        "attention":list_attention(connection,open_only=True,limit=25),
        "approvals":list_approvals(connection,state="pending",limit=25),
        "capabilities":[
            {"name":c["name"],"kind":c["kind"],"enabled":bool(c["enabled"]),"health":c["health"],
             "auth_status":c["auth_status"],"owner_approval_required":bool(c["owner_approval_required"])}
            for c in list_capabilities(connection)
        ],
        "sync":sync_status(connection),
        "reconciliation":[
            {"resource_key":r["resource_key"],"resource_type":r["resource_type"],
             "controller":r["controller"],"status":r["status"],"generation":r["generation"],
             "observed_generation":r["observed_generation"],"attempts":r["attempts"],
             "max_attempts":r["max_attempts"],"last_error":r["last_error"]}
            for r in list_resources(connection,limit=50)
        ],
        "verified_revenue":[
            x for x in list_payment_evidence(connection,limit=25)
            if x["authoritative"] and x["status"]=="confirmed"
        ],
    }

def _render(snapshot:dict)->str:
    w=snapshot["worker"]
    q=w.get("queue",{})
    rows="".join(
        f"<tr><td>{html.escape(a['kind'])}</td><td>{html.escape(a['severity'])}</td><td>{html.escape(a['source'])}</td><td>{html.escape(a['created_at'])}</td></tr>"
        for a in snapshot["attention"]
    ) or "<tr><td colspan='4'>None</td></tr>"
    caps="".join(
        f"<tr><td>{html.escape(c['name'])}</td><td>{html.escape(c['kind'])}</td><td>{html.escape(c['health'])}</td></tr>"
        for c in snapshot["capabilities"]
    ) or "<tr><td colspan='3'>None</td></tr>"
    return f"""<!doctype html><meta charset='utf-8'><title>LIFE OS</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:1000px}}table{{border-collapse:collapse;width:100%;margin-bottom:2rem}}td,th{{border:1px solid #bbb;padding:.4rem;text-align:left}}code{{background:#eee;padding:.1rem .3rem}}</style>
<h1>LIFE OS local control plane</h1>
<p>Worker heartbeat: <code>{html.escape(str(w.get('heartbeat')))}</code></p>
<p>Queue: <code>{html.escape(json.dumps(q,sort_keys=True))}</code></p>
<p>Pending approvals: <strong>{len(snapshot['approvals'])}</strong> · Verified revenue records: <strong>{len(snapshot['verified_revenue'])}</strong></p>
<h2>Owner attention</h2><table><tr><th>Type</th><th>Severity</th><th>Source</th><th>Created</th></tr>{rows}</table>
<h2>Capabilities</h2><table><tr><th>Name</th><th>Kind</th><th>Health</th></tr>{caps}</table>
<p>This dashboard is read-only and bound to 127.0.0.1.</p>"""

def serve_dashboard(db_path:str|Path,host:str="127.0.0.1",port:int=8765)->None:
    if host!="127.0.0.1":
        raise ValueError("control dashboard must bind strictly to 127.0.0.1")
    path=str(db_path)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args):
            return
        def do_GET(self):
            parsed=urlparse(self.path)
            c=connect(path); initialize(c)
            try:
                snap=control_snapshot(c)
            finally:
                c.close()
            if parsed.path=="/api/status":
                body=json.dumps(snap,sort_keys=True).encode()
                self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
            if parsed.path!="/":
                self.send_error(404); return
            body=_render(snap).encode()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    ThreadingHTTPServer((host,port),Handler).serve_forever()
