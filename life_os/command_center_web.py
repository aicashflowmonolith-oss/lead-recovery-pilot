"""Responsive server-rendered Central Command Hub UI."""
from __future__ import annotations

import html
import json
from typing import Any

COMMAND_CENTER_JS = r"""
let ownerGateKey='';
let ownerGateSnoozedUntil=0;

function safeReload(){
  const active=document.activeElement;
  const gate=document.getElementById('owner-gate');
  if((active && ['INPUT','TEXTAREA','SELECT'].includes(active.tagName)) || (gate && gate.open)) return;
  location.reload();
}
function parsePayload(raw){
  try{return JSON.parse(raw||'{}')}catch(_){return {}}
}
function make(tag,text,className){
  const el=document.createElement(tag);
  if(text!==undefined && text!==null) el.textContent=String(text);
  if(className) el.className=className;
  return el;
}
function csrfToken(){
  const input=document.querySelector("input[name='csrf']");
  return input ? input.value : '';
}
function dollars(cents){
  const value=Number(cents||0)/100;
  return new Intl.NumberFormat(undefined,{style:'currency',currency:'CAD'}).format(value);
}
function gateLabel(action,payload){
  const text=((payload.gate_type||'')+' '+(payload.title||'')+' '+(action||'')).toLowerCase();
  if(/credential|password|api key|secret|token/.test(text)) return 'Credential needed';
  if(/consent|legal|identity|verify identity|terms/.test(text)) return 'Consent or identity step needed';
  if(/purchase|spend|payment|pay|cost|budget/.test(text)) return 'Spending approval needed';
  if(/physical|plug|press|connect|device|hardware/.test(text)) return 'Physical action needed';
  return 'MONOLITH needs you';
}
function defaultInstruction(action,payload){
  const text=((payload.gate_type||'')+' '+(payload.title||'')+' '+(action||'')).toLowerCase();
  if(/credential|password|api key|secret|token/.test(text)){
    return 'Add the required credential only through the provider or Windows secure credential flow. Do not paste the secret into this popup. MONOLITH will keep the blocked branch waiting until the capability is verified.';
  }
  if(/consent|legal|identity|verify identity|terms/.test(text)){
    return 'Review the exact requested consent or identity step, then approve only if you want MONOLITH to continue that specific action.';
  }
  if(/purchase|spend|payment|pay|cost|budget/.test(text)){
    return 'Approve or deny this exact spend. Approval applies only to this recorded request; it is not blanket spending authority.';
  }
  if(/physical|plug|press|connect|device|hardware/.test(text)){
    return 'Do the single physical step shown here. Unrelated work continues while this branch waits.';
  }
  return 'Complete or decide only the exact step shown here. Unrelated work keeps running automatically.';
}
function maybeNotify(key,title,detail){
  if(!('Notification' in window) || Notification.permission!=='granted') return;
  const last=localStorage.getItem('life-os:last-owner-gate-notification');
  if(last===key) return;
  try{
    new Notification(title,{body:detail||'Open LIFE OS Command Center to continue.'});
    localStorage.setItem('life-os:last-owner-gate-notification',key);
  }catch(_){}
}
async function submitOwnerGate(action,fields,status){
  const token=csrfToken();
  if(!token){status.textContent='Approval control is unavailable. Reload Command Center.';return}
  const params=new URLSearchParams({csrf:token,action});
  Object.entries(fields||{}).forEach(([key,value])=>params.set(key,String(value)));
  status.textContent='Saving…';
  try{
    const response=await fetch('/action',{
      method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},
      body:params.toString(),
      redirect:'follow'
    });
    if(!response.ok) throw new Error('Request failed');
    ownerGateKey='';
    const dialog=document.getElementById('owner-gate');
    if(dialog && dialog.open) dialog.close();
    await refreshOwnerGate(true);
  }catch(_){
    status.textContent='Could not save that decision. Nothing was authorized; try again.';
  }
}
function renderOwnerGate(gate){
  const dialog=document.getElementById('owner-gate');
  if(!dialog) return;
  const payload=parsePayload(gate.item.payload_json);
  const action=gate.kind==='approval' ? gate.item.action : gate.item.source;
  const title=payload.title || gateLabel(action,payload);
  const why=payload.why || payload.summary || payload.reason || '';
  const instruction=payload.instructions || payload.instruction || defaultInstruction(action,payload);
  const key=gate.kind+':'+gate.item.id;
  ownerGateKey=key;

  const wrap=make('div',null,'gate-wrap');
  const eyebrow=make('div',gateLabel(action,payload),'gate-eyebrow');
  const heading=make('h2',title);
  const explain=make('p',why || 'This branch reached a step that MONOLITH is not authorized to perform for you.','gate-why');
  const steps=make('div',null,'gate-steps');
  steps.append(make('strong','What you need to do'),make('p',instruction));
  wrap.append(eyebrow,heading,explain,steps);

  const meta=make('div',null,'gate-meta');
  if(gate.kind==='approval'){
    meta.append(make('span','Risk: '+String(gate.item.risk||'unspecified')));
    meta.append(make('span','Cost: '+dollars(gate.item.cost_cents)));
    if(gate.item.expires_at) meta.append(make('span','Expires: '+String(gate.item.expires_at)));
  }else{
    meta.append(make('span','Source: '+String(gate.item.source||'system')));
    meta.append(make('span','Severity: '+String(gate.item.severity||'info')));
  }
  wrap.append(meta);

  const status=make('div','Unrelated work keeps running while this waits.','gate-status');
  const actions=make('div',null,'gate-actions');
  if(gate.kind==='approval'){
    const approve=make('button','Approve this exact action','gate-approve');
    approve.type='button';
    approve.addEventListener('click',()=>submitOwnerGate('approval_decide',{approval_id:gate.item.id,decision:'approved'},status));
    const deny=make('button','Deny','gate-deny');
    deny.type='button';
    deny.addEventListener('click',()=>submitOwnerGate('approval_decide',{approval_id:gate.item.id,decision:'denied'},status));
    actions.append(approve,deny);
  }else{
    const done=make('button','I did this','gate-approve');
    done.type='button';
    done.addEventListener('click',()=>submitOwnerGate('attention_ack',{attention_id:gate.item.id},status));
    actions.append(done);
  }
  const later=make('button','Later','gate-later');
  later.type='button';
  later.addEventListener('click',()=>{
    ownerGateSnoozedUntil=Date.now()+10*60*1000;
    if(dialog.open) dialog.close();
  });
  actions.append(later);

  if('Notification' in window && Notification.permission==='default'){
    const enable=make('button','Enable future pop-up alerts','gate-later');
    enable.type='button';
    enable.addEventListener('click',async()=>{
      const permission=await Notification.requestPermission();
      enable.textContent=permission==='granted'?'Pop-up alerts enabled':'Browser notifications not enabled';
      if(permission==='granted') maybeNotify(key,title,instruction);
    });
    actions.append(enable);
  }
  wrap.append(status,actions);
  dialog.replaceChildren(wrap);
  maybeNotify(key,title,instruction);
  if(Date.now()>=ownerGateSnoozedUntil && !dialog.open) dialog.showModal();
}
async function refreshOwnerGate(force=false){
  try{
    const response=await fetch('/api/status',{cache:'no-store'});
    if(!response.ok) return;
    const snapshot=await response.json();
    const approval=(snapshot.approvals||[])[0]||null;
    const attention=(snapshot.attention||[]).find(item=>item.kind==='human_gate')||null;
    const gate=approval ? {kind:'approval',item:approval} : (attention ? {kind:'attention',item:attention} : null);
    const dialog=document.getElementById('owner-gate');
    if(!gate){
      ownerGateKey='';
      if(dialog && dialog.open) dialog.close();
      return;
    }
    const key=gate.kind+':'+gate.item.id;
    if(!force && key===ownerGateKey && dialog && dialog.open) return;
    renderOwnerGate(gate);
  }catch(_){}
}
window.addEventListener('load',()=>{
  refreshOwnerGate();
  setInterval(refreshOwnerGate,5000);
  setInterval(safeReload,10000);
});
"""

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)

def _meta(raw: str) -> str:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    if not value:
        return ""
    return _e(json.dumps(value, sort_keys=True))[:500]

def render(snapshot: dict[str, Any], csrf: str, message: str = "") -> str:
    counts = snapshot["counts"]
    worker = snapshot["worker"]
    gateway = snapshot["gateway"]
    flash = f"<div class='flash'>{_e(message)}</div>" if message else ""

    activity = "".join(
        f"<div class='row activity { _e(item['level']) }'>"
        f"<div><strong>{_e(item['kind'])}</strong><span>{_e(item['occurred_at'])}</span></div>"
        f"<p>{_e(item['message'])}</p><small>{_meta(item['metadata_json'])}</small></div>"
        for item in snapshot["activity"]
    ) or "<p class='empty'>No command-center activity yet.</p>"

    tasks = "".join(
        f"<div class='row'><div><strong>{_e(item['name'])}</strong>"
        f"<span>{_e(item['schedule_kind'])} · next {_e(item['next_run_at'])}</span></div>"
        f"<p>{_e(item['directive'])}</p>"
        f"<small>risk {_e(item['risk_level'])} · retries {item['retry_count']}/{item['max_retries']}</small></div>"
        for item in snapshot["scheduled_tasks"]
    ) or "<p class='empty'>No active scheduled tasks.</p>"

    reminders = "".join(
        f"<div class='row'><div><strong>{_e(item['message'])}</strong>"
        f"<span>{_e(item['remind_at'])}</span></div></div>"
        for item in snapshot["pending_reminders"]
    ) or "<p class='empty'>No pending reminders.</p>"

    errors = "".join(
        f"<div class='row error {_e(item['severity'])}'><div><strong>{_e(item['error_type'])}</strong>"
        f"<span>{_e(item['occurred_at'])}</span></div><p>{_e(item['message'])}</p>"
        f"<small>{_e(item['severity'])} · {'retryable' if item['retryable'] else 'terminal'}</small></div>"
        for item in snapshot["system_errors"]
    ) or "<p class='empty'>No open system errors.</p>"

    agents = "".join(
        f"<div class='agent'><strong>{_e(item['agent_key'])}</strong>"
        f"<span class='pill'>{_e(item['status'])}</span>"
        f"<small>{_e(item['updated_at'])}</small></div>"
        for item in snapshot["agent_states"]
    ) or "<p class='empty'>No agent state has been recorded yet.</p>"

    heartbeat = worker["heartbeat"]
    pulse = "online" if heartbeat else "offline"
    gateway_label = (
        f"LiteLLM · {_e(gateway['model'])}" if gateway["configured"]
        else "Native / capability fabric"
    )
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='theme-color' content='#08111b'><title>LIFE OS · Central Command</title>
<script src='/command-center.js' defer></script>
<style>
:root{{--bg:#06101a;--panel:#0c1824;--line:#1c3142;--text:#eef7ff;--muted:#8da4b8;--accent:#6ed6ff;--ok:#6de2b9;--warn:#ffd171;--bad:#ff7d88}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 80% 0,#102a3a 0,transparent 32rem),var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
a{{color:var(--accent);text-decoration:none}}header{{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;gap:16px;align-items:center;padding:14px max(18px,4vw);background:#07111ddd;border-bottom:1px solid var(--line);backdrop-filter:blur(16px)}}
.brand{{font-weight:800;letter-spacing:.08em}}main{{max-width:1500px;margin:auto;padding:24px max(16px,4vw) 48px}}.top{{display:grid;grid-template-columns:1.5fr 1fr;gap:16px;align-items:end}}h1{{font-size:clamp(32px,5vw,62px);line-height:1;margin:8px 0}}.sub{{color:var(--muted);max-width:760px}}
.command{{display:flex;gap:10px;padding:10px;border:1px solid #2f6680;border-radius:16px;background:#0b1a27;margin-top:18px}}.command input{{flex:1;background:transparent;border:0;color:var(--text);font-size:17px;outline:0;padding:10px}}button{{border:0;border-radius:11px;padding:10px 18px;background:linear-gradient(135deg,var(--accent),var(--ok));font-weight:800;color:#041018;cursor:pointer}}
.metrics{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:22px 0}}.metric,.card{{border:1px solid var(--line);background:linear-gradient(180deg,#0d1b28e8,#091621e8);border-radius:16px;box-shadow:0 18px 50px #0004}}.metric{{padding:14px}}.metric strong{{display:block;font-size:26px}}.metric span,.row span,small{{color:var(--muted);font-size:11px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}.card{{padding:17px;min-width:0}}.card.wide{{grid-column:1/-1}}.card h2{{margin:0 0 12px;font-size:15px;display:flex;justify-content:space-between}}.scroll{{max-height:440px;overflow:auto}}.row{{padding:12px 0;border-top:1px solid #162b3a;overflow-wrap:anywhere}}.row:first-child{{border-top:0}}.row>div{{display:flex;justify-content:space-between;gap:10px}}.row p{{margin:6px 0}}.activity.warning,.error.warning{{border-left:3px solid var(--warn);padding-left:10px}}.activity.error,.activity.critical,.error.error,.error.critical{{border-left:3px solid var(--bad);padding-left:10px}}
.agent{{display:grid;grid-template-columns:1fr auto;gap:5px;padding:9px 0;border-top:1px solid #162b3a}}.agent small{{grid-column:1/-1}}.pill{{border:1px solid var(--line);border-radius:999px;padding:3px 8px;color:var(--ok)}}.flash{{margin:12px 0;padding:11px 14px;background:#123c34;border:1px solid #266d5c;border-radius:12px}}.empty{{color:var(--muted)}}.danger{{color:var(--bad)}}code{{color:#bfe9ff}}
#owner-gate{{width:min(680px,calc(100vw - 28px));border:1px solid #315066;border-radius:20px;background:#091722;color:var(--text);padding:0;box-shadow:0 28px 90px #000b}}#owner-gate::backdrop{{background:#02070bcc;backdrop-filter:blur(5px)}}.gate-wrap{{padding:24px}}.gate-eyebrow{{color:var(--warn);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}}.gate-wrap h2{{font-size:28px;margin:6px 0 10px}}.gate-why{{font-size:15px;color:#d8e8f4}}.gate-steps{{margin:18px 0;padding:16px;border:1px solid #244255;border-radius:14px;background:#0d1d2a}}.gate-steps p{{white-space:pre-wrap;margin:8px 0 0}}.gate-meta{{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}}.gate-meta span{{border:1px solid var(--line);border-radius:999px;padding:5px 9px;color:var(--muted);font-size:12px}}.gate-status{{color:var(--muted);margin:12px 0}}.gate-actions{{display:flex;flex-wrap:wrap;gap:9px;margin-top:16px}}.gate-deny{{background:#44232a;color:#ffdce0}}.gate-later{{background:#162735;color:#dceaf4}}
@media(max-width:900px){{.top,.grid{{grid-template-columns:1fr}}.metrics{{grid-template-columns:repeat(2,1fr)}}.card.wide{{grid-column:auto}}}}
@media(max-width:520px){{header{{align-items:flex-start;flex-direction:column}}.metrics{{grid-template-columns:1fr}}.command{{display:grid;grid-template-columns:1fr auto}}.gate-actions button{{width:100%}}}}
</style></head><body>
<header><div class='brand'>MONOLITH / LIFE OS · CENTRAL COMMAND</div><nav><a href='/'>LIFE OS</a></nav></header>
<dialog id='owner-gate' aria-labelledby='owner-gate-title'></dialog>
<main>{flash}
<section class='top'><div><div class='sub'>Persistent local control plane</div><h1>Command Center</h1>
<p class='sub'>Native-first routing, durable schedules, agent state, errors, approvals and background activity in one cockpit.</p></div>
<div><strong>Worker: <span class='{pulse}'>{pulse}</span></strong><br><span class='sub'>Routing: {gateway_label}</span></div></section>
<form method='post' action='/command-center' class='command'>
<input type='hidden' name='csrf' value='{_e(csrf)}'><input type='hidden' name='action' value='command_submit'>
<input name='text' maxlength='4000' autocomplete='off' autofocus placeholder='Give MONOLITH / LIFE OS a command…'>
<button>Execute</button></form>
<section class='metrics'>
<div class='metric'><strong>{counts['scheduled_tasks']}</strong><span>scheduled tasks</span></div>
<div class='metric'><strong>{counts['pending_reminders']}</strong><span>pending reminders</span></div>
<div class='metric'><strong>{counts['open_errors']}</strong><span>open errors</span></div>
<div class='metric'><strong class='{'danger' if counts['critical_errors'] else ''}'>{counts['critical_errors']}</strong><span>critical errors</span></div>
<div class='metric'><strong>{counts['agents']}</strong><span>tracked agents</span></div>
</section>
<div class='grid'>
<section class='card wide'><h2><span>Live Activity Feed</span><span>auto-refresh 10s</span></h2><div class='scroll'>{activity}</div></section>
<section class='card'><h2><span>Task Board</span><span>{counts['scheduled_tasks']}</span></h2><div class='scroll'>{tasks}</div></section>
<section class='card'><h2><span>Reminder Board</span><span>{counts['pending_reminders']}</span></h2><div class='scroll'>{reminders}</div></section>
<section class='card'><h2><span>System Health / Errors</span><span>{counts['open_errors']}</span></h2><div class='scroll'>{errors}</div></section>
<section class='card'><h2><span>Agent States</span><span>{counts['agents']}</span></h2><div class='scroll'>{agents}</div></section>
</div></main></body></html>"""