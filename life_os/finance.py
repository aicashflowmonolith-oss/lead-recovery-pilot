"""Minimal personal finance ledger using integer cents."""
from __future__ import annotations
import sqlite3
from datetime import date
from .foundation import bind_legacy_entity

def add_account(c:sqlite3.Connection,name:str,balance_cents:int=0)->int:
    if not name.strip(): raise ValueError("account name required")
    cur=c.execute("INSERT INTO accounts(name,balance_cents) VALUES(?,?)",(name.strip(),balance_cents))
    account_id=int(cur.lastrowid)
    bind_legacy_entity(c,table_name="accounts",row_id=account_id,entity_type="account",domain_key="money",title=name.strip(),metadata={"balance_cents":balance_cents})
    c.commit(); return account_id

def transact(c:sqlite3.Connection,account_id:int,amount_cents:int,category:str,note:str="",occurred_on:date|None=None)->int:
    if not category.strip(): raise ValueError("category required")
    day=(occurred_on or date.today()).isoformat()
    cur=c.execute("INSERT INTO transactions(account_id,amount_cents,category,occurred_on,note) VALUES(?,?,?,?,?)",(account_id,amount_cents,category.strip(),day,note))
    c.execute("UPDATE accounts SET balance_cents=balance_cents+? WHERE id=?",(amount_cents,account_id))
    if c.execute("SELECT changes()").fetchone()[0] != 1: c.rollback(); raise ValueError("account not found")
    account=c.execute("SELECT * FROM accounts WHERE id=?",(account_id,)).fetchone()
    bind_legacy_entity(c,table_name="accounts",row_id=account_id,entity_type="account",domain_key="money",title=account["name"],metadata={"balance_cents":account["balance_cents"]})
    c.commit(); return int(cur.lastrowid)

def balances(c:sqlite3.Connection)->list[tuple[int,str,int]]:
    return [(r["id"],r["name"],r["balance_cents"]) for r in c.execute("SELECT * FROM accounts ORDER BY id")]
