"""
core/job_queue.py — Enqueue/dequeue helpers for the job_queue table (spec Section 5)

Thin wrappers around the supabase_client functions, exposed with the names
the spec calls (enqueue / dequeue / complete / fail / pending_count).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from core.supabase_client import (
    enqueue_job,
    dequeue_job,
    complete_job,
    fail_job,
    supabase,
)


def enqueue(job_type: str, platform: str = None, payload: dict = None) -> dict:
    """
    Add a new 'pending' job to the queue.

    Returns the created job row.
    """
    return enqueue_job(job_type=job_type, platform=platform, payload=payload or {})


def dequeue(job_type: str) -> dict | None:
    """
    Claim the oldest pending job of the given type.
    Sets status to 'processing'. Returns the job row or None if empty.
    """
    return dequeue_job(job_type)


def complete(job_id: str) -> dict:
    """Mark a job as 'done'."""
    return complete_job(job_id)


def fail(job_id: str, error_message: str) -> dict:
    """Mark a job as 'failed' with an error message."""
    return fail_job(job_id, error_message)


def pending_count(job_type: str = None) -> int:
    """
    Count pending jobs. Optionally filter by job_type.
    Returns 0 on any DB error (graceful for callers that run before table exists).
    """
    try:
        q = supabase.table("job_queue").select("id", count="exact").eq("status", "pending")
        if job_type:
            q = q.eq("job_type", job_type)
        result = q.execute()
        return result.count or 0
    except Exception:
        return 0
