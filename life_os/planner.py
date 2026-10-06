"""Deterministic daily prioritization engine."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from .models import Task

@dataclass(frozen=True)
class PlannedTask:
    task: Task
    score: float

def score_task(task:Task,today:date|None=None)->float:
    today=today or date.today()
    score=float(task.priority)
    if task.due_date:
        days=(task.due_date-today).days
        if days < 0: score += 50
        elif days == 0: score += 40
        elif days <= 3: score += 25
        elif days <= 7: score += 10
    score += max(0,20-(task.effort_minutes/15))
    return round(score,2)

def make_plan(tasks:list[Task],available_minutes:int=480,today:date|None=None)->list[PlannedTask]:
    if available_minutes < 0: raise ValueError("available minutes cannot be negative")
    ranked=sorted((PlannedTask(t,score_task(t,today)) for t in tasks),key=lambda x:(-x.score,x.task.effort_minutes,x.task.id))
    chosen=[]; used=0
    for item in ranked:
        if used + item.task.effort_minutes <= available_minutes:
            chosen.append(item); used += item.task.effort_minutes
    return chosen

def next_action(tasks:list[Task],today:date|None=None)->PlannedTask|None:
    plan=make_plan(tasks,10**9,today)
    return plan[0] if plan else None
