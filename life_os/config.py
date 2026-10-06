"""Runtime paths and conservative defaults."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Config:
    home:Path
    db:Path
    backups:Path
    daily_available_minutes:int=480
    cash_reserve_cents:int=0

def default_config()->Config:
    home=Path.home()/".life-os"
    return Config(home,home/"life.db",home/"backups")
