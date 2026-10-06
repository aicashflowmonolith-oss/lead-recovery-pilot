"""Conservative rule engine that turns state into planning signals."""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass

@dataclass(frozen=True)
class Signal:
    kind:str
    message:str
    priority:int

def signals(c:sqlite3.Connection)->list[Signal]:
    out=[]
    check=c.execute("SELECT * FROM checkins ORDER BY occurred_on DESC LIMIT 1").fetchone()
    if check:
        if check["sleep_hours"] is not None and check["sleep_hours"] < 6:
            out.append(Signal("recovery","Sleep was under 6 hours; protect recovery and avoid unnecessary overload.",90))
        if check["pain"] is not None and check["pain"] >= 7:
            out.append(Signal("pain","High pain was recorded; avoid automatically escalating physical workload.",95))
        if check["energy"] is not None and check["energy"] <= 3:
            out.append(Signal("energy","Energy is low; favor essential and short tasks.",80))
    return sorted(out,key=lambda x:-x.priority)
