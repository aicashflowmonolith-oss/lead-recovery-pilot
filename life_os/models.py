"""Core domain models."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date

@dataclass(frozen=True)
class Goal:
    id: int
    title: str
    priority: int
    status: str

@dataclass(frozen=True)
class Task:
    id: int
    title: str
    priority: int
    effort_minutes: int
    due_date: date | None
    status: str
    goal_id: int | None
