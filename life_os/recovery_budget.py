"""Database-enforced budget for repeated controller-owned job recovery.

Infrastructure interruption must not silently create an infinite logical retry
budget. The recovery controller may resume the same exact job a small number of
times, with increasing backoff, while unrelated lanes continue. After the
bounded budget is exhausted the job and its engineering run are parked for
normal reconciliation instead of churning indefinitely.
"""
from __future__ import annotations

import sqlite3

MAX_INFRASTRUCTURE_RESUMES = 5
MAX_BACKOFF_SECONDS = 900

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS recovery_job_resumes (
    job_id INTEGER PRIMARY KEY REFERENCES worker_jobs(id),
    resume_count INTEGER NOT NULL DEFAULT 0 CHECK(resume_count >= 0),
    last_resumed_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_bound_controller_owned_engineering_resume
AFTER UPDATE OF state, last_error ON worker_jobs
WHEN OLD.state='running'
 AND NEW.state='retry'
 AND NEW.kind='engineering.build'
 AND NEW.last_error='controller-owned worker interrupted; resume original job'
BEGIN
    INSERT INTO recovery_job_resumes(job_id,resume_count,last_resumed_at)
    VALUES(NEW.id,1,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
    ON CONFLICT(job_id) DO UPDATE SET
        resume_count=resume_count+1,
        last_resumed_at=excluded.last_resumed_at;

    UPDATE worker_jobs
    SET state=CASE
            WHEN (SELECT resume_count FROM recovery_job_resumes WHERE job_id=NEW.id) > {MAX_INFRASTRUCTURE_RESUMES}
            THEN 'dead' ELSE 'retry' END,
        attempts=CASE
            WHEN (SELECT resume_count FROM recovery_job_resumes WHERE job_id=NEW.id) > {MAX_INFRASTRUCTURE_RESUMES}
            THEN OLD.attempts
            WHEN OLD.attempts > 0 THEN OLD.attempts-1 ELSE 0 END,
        max_attempts=OLD.max_attempts,
        available_at=CASE
            WHEN (SELECT resume_count FROM recovery_job_resumes WHERE job_id=NEW.id) > {MAX_INFRASTRUCTURE_RESUMES}
            THEN NEW.available_at
            ELSE strftime(
                '%Y-%m-%dT%H:%M:%f+00:00','now',
                '+' || min(
                    {MAX_BACKOFF_SECONDS},
                    30 * (1 << ((SELECT resume_count FROM recovery_job_resumes WHERE job_id=NEW.id)-1))
                ) || ' seconds'
            ) END,
        last_error=CASE
            WHEN (SELECT resume_count FROM recovery_job_resumes WHERE job_id=NEW.id) > {MAX_INFRASTRUCTURE_RESUMES}
            THEN 'infrastructure resume budget exhausted; engineering objective parked for reconciliation'
            ELSE 'controller-owned worker interrupted; bounded infrastructure resume scheduled' END
    WHERE id=NEW.id;

    UPDATE engineering_runs
    SET state='failed',
        updated_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
        finished_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
        failure_reason='Infrastructure recovery budget exhausted; resource/capacity reconciliation required'
    WHERE id=CAST(json_extract(NEW.payload_json,'$.run_id') AS INTEGER)
      AND (SELECT resume_count FROM recovery_job_resumes WHERE job_id=NEW.id) > {MAX_INFRASTRUCTURE_RESUMES}
      AND state IN ('queued','building','verifying','waiting_provider');

    INSERT INTO events(kind,occurred_at,payload_json)
    SELECT
        'engineering.run.infrastructure_budget_exhausted',
        strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
        json_object(
            'job_id', NEW.id,
            'run_id', CAST(json_extract(NEW.payload_json,'$.run_id') AS INTEGER),
            'resume_count', (SELECT resume_count FROM recovery_job_resumes WHERE job_id=NEW.id)
        )
    WHERE (SELECT resume_count FROM recovery_job_resumes WHERE job_id=NEW.id) > {MAX_INFRASTRUCTURE_RESUMES};
END;
"""


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()
