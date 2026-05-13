"""
core/activity_logger.py — Persistent event feed for the dashboard.

Single public function: log_activity(agent, event_type, message, metadata=None)

Every meaningful agent action should call this so the dashboard's activity feed
has real data.  Failures are swallowed and printed — a logging failure must never
crash the pipeline.

Event types (must match activity_log table CHECK constraint):
  design_generated  — image successfully produced by gpt-image-1
  design_approved   — QA passed
  design_rejected   — QA failed
  order_received    — new Fiverr order detected via IMAP
  order_fulfilled   — thumbnails generated and delivery email sent
  listing_published — design pushed to Etsy/Printify
  research_complete — research brief saved to Supabase
  error             — any agent-level exception
  sale              — a sale was recorded
  proposal_found    — scout found a new opportunity
  system            — scheduler start/stop, config changes
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.supabase_client import supabase


def log_activity(
    agent: str,
    event_type: str,
    message: str,
    metadata: dict | None = None,
) -> None:
    """
    Insert one row into activity_log.

    Args:
        agent:      Name of the calling agent, e.g. 'design_agent'.
        event_type: One of the valid event_type values listed above.
        message:    Human-readable one-line description (max ~200 chars).
        metadata:   Optional dict with extra context (stored as JSONB).

    Returns:
        None — failures are caught and printed, never raised.
    """
    valid_types = {
        "design_generated", "design_approved", "design_rejected",
        "order_received",   "order_fulfilled",  "listing_published",
        "research_complete","error",             "sale",
        "proposal_found",   "system",
    }
    if event_type not in valid_types:
        print(f"[activity_logger] WARNING: unknown event_type '{event_type}' — using 'system'")
        event_type = "system"

    row: dict = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "agent":      agent[:100],
        "event_type": event_type,
        "message":    message[:500],
    }
    if metadata:
        row["metadata"] = metadata

    try:
        supabase.table("activity_log").insert(row).execute()
    except Exception as e:
        # Never crash the caller — just print
        print(f"[activity_logger] WARNING: failed to log activity: {e}")


if __name__ == "__main__":
    log_activity(
        agent="test",
        event_type="system",
        message="activity_logger test — if this shows up in the dashboard it's working",
        metadata={"test": True},
    )
    print("[activity_logger] Test event logged.")
