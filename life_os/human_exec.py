"""Human service providers as a gated execution capability."""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Any
from .attention import emit_attention, request_approval
from .boundaries import decision, validate_external_payload
from .events import append_event

def _now()->str:
    return datetime.now(timezone.utc).isoformat()

def upsert_human_provider(connection:sqlite3.Connection, *, name:str, category:str, service_area:str="",
                          contact_ref:str="", availability_note:str="", quote_cents:int|None=None,
                          reputation_score:float=0.0,reputation_evidence:str="",
                          licensing_insurance_note:str="",privacy_exposure:str="low",reversible:bool=True,
                          cancellation_terms:str="",verification_method:str="",enabled:bool=True)->int:
    if quote_cents is not None and quote_cents<0:
        raise ValueError("quote_cents cannot be negative")
    if not 0<=reputation_score<=1:
        raise ValueError("reputation_score must be 0..1")
    connection.execute(
        """INSERT INTO human_providers(name,category,service_area,contact_ref,availability_note,quote_cents,
        reputation_score,reputation_evidence,licensing_insurance_note,privacy_exposure,reversible,
        cancellation_terms,verification_method,enabled,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(name,category,service_area) DO UPDATE SET contact_ref=excluded.contact_ref,
        availability_note=excluded.availability_note,quote_cents=excluded.quote_cents,
        reputation_score=excluded.reputation_score,reputation_evidence=excluded.reputation_evidence,
        licensing_insurance_note=excluded.licensing_insurance_note,privacy_exposure=excluded.privacy_exposure,
        reversible=excluded.reversible,cancellation_terms=excluded.cancellation_terms,
        verification_method=excluded.verification_method,enabled=excluded.enabled,updated_at=excluded.updated_at""",
        (name,category,service_area,contact_ref,availability_note,quote_cents,reputation_score,reputation_evidence,
         licensing_insurance_note,privacy_exposure,int(reversible),cancellation_terms,verification_method,int(enabled),_now()),
    )
    connection.commit()
    row=connection.execute(
        "SELECT id FROM human_providers WHERE name=? AND category=? AND service_area=?",
        (name,category,service_area),
    ).fetchone()
    return int(row["id"])

def compare_human_providers(connection:sqlite3.Connection,category:str,limit:int=20)->list[dict[str,Any]]:
    rows=connection.execute(
        """SELECT * FROM human_providers WHERE category=? AND enabled=1
        ORDER BY reputation_score DESC,CASE WHEN quote_cents IS NULL THEN 1 ELSE 0 END,quote_cents ASC,name LIMIT ?""",
        (category,limit),
    ).fetchall()
    return [dict(r) for r in rows]

def request_human_executor(connection:sqlite3.Connection,provider_id:int,task_summary:str)->tuple[int,bool]:
    task_summary=task_summary.strip()
    if not 1<=len(task_summary)<=1000:
        raise ValueError("task_summary must be 1..1000 characters")
    boundary=decision(purpose="human_service_hire",explicit_owner_approval=False)
    if boundary.allowed:
        raise AssertionError("human-service hire must remain owner-gated")
    validate_external_payload({"task_summary":task_summary})
    row=connection.execute("SELECT * FROM human_providers WHERE id=? AND enabled=1",(provider_id,)).fetchone()
    if row is None:
        raise ValueError("provider not found or disabled")
    fingerprint=f"human_executor:{provider_id}:{task_summary.strip().lower()}"
    approval_id,created=request_approval(
        connection,fingerprint=fingerprint,action=f"hire human provider: {row['name']}",
        risk="external_human_paid_action",cost_cents=row["quote_cents"] or 0,
        payload={"provider_id":provider_id,"category":row["category"],"task_summary":task_summary},
    )
    if created:
        emit_attention(
            connection,fingerprint=f"attention:{fingerprint}",kind="human_executor_approval",
            severity="important",source="life-os.human_exec",
            payload={"approval_id":approval_id,"provider":row["name"],"task_summary":task_summary},
        )
        append_event(connection,"human_executor.approval_requested",{"approval_id":approval_id,"provider_id":provider_id})
    return approval_id,created
