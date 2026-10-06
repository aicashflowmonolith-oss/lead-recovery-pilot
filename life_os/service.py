"""Application service joining LIFE OS modules."""
from __future__ import annotations
import sqlite3
from datetime import date
from .events import append_event
from .planner import make_plan,next_action
from .store import add_goal,add_task,complete_task,list_open_tasks
from .routines import tasks_for_day

class LifeOS:
    def __init__(self,c:sqlite3.Connection): self.c=c
    def create_goal(self,title:str,priority:int=50):
        g=add_goal(self.c,title,priority); append_event(self.c,"goal.created",{"goal_id":g.id}); return g
    def create_task(self,title:str,priority:int=50,effort_minutes:int=30,due_date:date|None=None,goal_id:int|None=None):
        t=add_task(self.c,title,priority,effort_minutes,due_date,goal_id); append_event(self.c,"task.created",{"task_id":t.id}); return t
    def candidates(self,day:date|None=None):
        d=day or date.today()
        return list_open_tasks(self.c)+tasks_for_day(self.c,d)
    def today(self,available_minutes:int=480,day:date|None=None):
        d=day or date.today()
        return make_plan(self.candidates(d),available_minutes,d)
    def now(self,day:date|None=None):
        d=day or date.today()
        return next_action(self.candidates(d),d)
    def finish(self,task_id:int)->bool:
        if task_id < 0:
            append_event(self.c,"routine.completed",{"routine_id":-task_id,"date":date.today().isoformat()})
            return True
        ok=complete_task(self.c,task_id)
        if ok: append_event(self.c,"task.completed",{"task_id":task_id})
        return ok
