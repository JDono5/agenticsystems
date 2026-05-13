"""
core/scheduler_conditions.py — Pre-flight condition checks for every scheduled agent.

Each check_*() function returns:
  {"should_run": bool, "reason": str, "extra": dict}

The scheduler calls these before running each agent and logs the reason when
skipping.  The dashboard /scheduler/conditions endpoint calls all of them to
show the current run-readiness of every agent at a glance.

Design principles:
  - Every check is independently safe — DB failures default to should_run=True
    so a broken connection never permanently blocks agents.
  - All Supabase queries use try/except so a schema mismatch won't crash the
    scheduler process.
  - Checks read only — they never write anything.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.supabase_client import supabase
from core.spend_monitor import check_cap

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_count(q) -> int:
    try:
        return q.execute().count or 0
    except Exception:
        return 0

def _safe_data(q) -> list:
    try:
        return q.execute().data or []
    except Exception:
        return []

def _month_spend_pct() -> float:
    """Return current month spend as a fraction 0.0–1.0 of the cap."""
    try:
        cap = float(os.getenv("MONTHLY_SPEND_CAP", "100"))
        if cap <= 0:
            return 0.0
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()
        rows = _safe_data(
            supabase.table("cost_log").select("cost_usd").gte("timestamp", month_start)
        )
        spent = sum(float(r["cost_usd"]) for r in rows)
        return spent / cap
    except Exception:
        return 0.0

def _today_start() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00+00:00")

def _week_start() -> str:
    now    = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday.isoformat()


# ─── Individual condition checks ──────────────────────────────────────────────

def check_research() -> dict:
    """
    Skip research if:
    - 3+ unused briefs from today already exist
    - Monthly spend > 70% of cap
    """
    try:
        today = _today_start()

        # Count briefs created today
        briefs_today = _safe_count(
            supabase.table("research_briefs")
            .select("id", count="exact")
            .gte("created_at", today)
        )

        if briefs_today >= 3:
            return {
                "should_run": False,
                "reason": f"Already have {briefs_today} briefs from today — skipping to avoid over-research",
                "extra": {"briefs_today": briefs_today},
            }

        spend_pct = _month_spend_pct()
        if spend_pct > 0.70:
            return {
                "should_run": False,
                "reason": f"Monthly spend at {spend_pct*100:.0f}% of cap — pausing research above 70%",
                "extra": {"spend_pct": round(spend_pct, 3)},
            }

        return {
            "should_run": True,
            "reason": f"{briefs_today} briefs today, spend at {spend_pct*100:.0f}% — OK to run",
            "extra": {"briefs_today": briefs_today, "spend_pct": round(spend_pct, 3)},
        }
    except Exception as e:
        return {"should_run": True, "reason": f"Condition check failed ({e}) — running anyway", "extra": {}}


def check_design() -> dict:
    """
    Skip design if 8+ approved unpublished designs exist or spend > 80%.
    Returns extra.reduced_target=4 if 4–7 approved designs exist.
    """
    try:
        approved = _safe_count(
            supabase.table("designs")
            .select("id", count="exact")
            .eq("status", "approved")
            .eq("platform", "etsy")
        )

        if approved >= 8:
            return {
                "should_run": False,
                "reason": f"{approved} approved unpublished designs already queued — no more needed",
                "extra": {"approved_unpublished": approved},
            }

        spend_pct = _month_spend_pct()
        if spend_pct > 0.80:
            return {
                "should_run": False,
                "reason": f"Monthly spend at {spend_pct*100:.0f}% of cap — pausing design above 80%",
                "extra": {"spend_pct": round(spend_pct, 3)},
            }

        reduced_target = None
        if 4 <= approved < 8:
            reduced_target = 4
            return {
                "should_run": True,
                "reason": f"{approved} approved designs queued — running with reduced target of 4",
                "extra": {
                    "approved_unpublished": approved,
                    "reduced_target": reduced_target,
                    "spend_pct": round(spend_pct, 3),
                },
            }

        return {
            "should_run": True,
            "reason": f"{approved} approved designs queued, spend at {spend_pct*100:.0f}% — running full target",
            "extra": {"approved_unpublished": approved, "spend_pct": round(spend_pct, 3)},
        }
    except Exception as e:
        return {"should_run": True, "reason": f"Condition check failed ({e}) — running anyway", "extra": {}}


def check_publisher() -> dict:
    """
    Skip publisher if:
    - No approved unpublished designs exist
    - DRAFT_MODE=true and 10+ draft listings pending review
    - ETSY_ACCESS_TOKEN is blank
    """
    try:
        etsy_token = os.getenv("ETSY_ACCESS_TOKEN", "").strip()
        if not etsy_token:
            return {
                "should_run": False,
                "reason": "ETSY_ACCESS_TOKEN not configured — Etsy publisher unavailable",
                "extra": {"etsy_configured": False},
            }

        approved = _safe_count(
            supabase.table("designs")
            .select("id", count="exact")
            .eq("status", "approved")
            .eq("platform", "etsy")
        )
        if approved == 0:
            return {
                "should_run": False,
                "reason": "No approved designs ready to publish",
                "extra": {"approved_count": 0},
            }

        draft_mode = os.getenv("DRAFT_MODE", "true").lower() == "true"
        if draft_mode:
            draft_count = _safe_count(
                supabase.table("listings")
                .select("id", count="exact")
                .eq("status", "draft")
            )
            if draft_count >= 10:
                return {
                    "should_run": False,
                    "reason": f"DRAFT_MODE=true with {draft_count} draft listings pending review — review before publishing more",
                    "extra": {"draft_count": draft_count, "draft_mode": True},
                }

        return {
            "should_run": True,
            "reason": f"{approved} approved designs ready — publisher can proceed",
            "extra": {"approved_count": approved},
        }
    except Exception as e:
        return {"should_run": True, "reason": f"Condition check failed ({e}) — running anyway", "extra": {}}


def check_fiverr_orders() -> dict:
    """
    Skip if GMAIL_APP_PASSWORD is not configured or still placeholder.
    Always runs every 4 hours regardless of other conditions.
    """
    try:
        pwd = os.getenv("GMAIL_APP_PASSWORD", "").strip()
        placeholder_values = {"", "xxxx-xxxx-xxxx-xxxx", "paste-your-16-char-password-here"}
        if pwd in placeholder_values:
            return {
                "should_run": False,
                "reason": "GMAIL_APP_PASSWORD not configured — set it in .env to enable Fiverr order polling",
                "extra": {"gmail_configured": False},
            }

        return {
            "should_run": True,
            "reason": "Gmail IMAP configured — polling for orders",
            "extra": {"gmail_configured": True},
        }
    except Exception as e:
        return {"should_run": True, "reason": f"Condition check failed ({e}) — running anyway", "extra": {}}


def check_performance() -> dict:
    """
    Skip if no active listings exist or fewer than 14 days since first listing.
    """
    try:
        active = _safe_count(
            supabase.table("listings")
            .select("id", count="exact")
            .eq("status", "active")
        )
        if active == 0:
            return {
                "should_run": False,
                "reason": "No active listings yet — performance agent has nothing to evaluate",
                "extra": {"active_listings": 0},
            }

        # Check age of first active listing
        first = _safe_data(
            supabase.table("listings")
            .select("published_at")
            .eq("status", "active")
            .order("published_at")
            .limit(1)
        )
        if first and first[0].get("published_at"):
            published = datetime.fromisoformat(
                first[0]["published_at"].replace("Z", "+00:00")
            )
            age_days = (datetime.now(timezone.utc) - published).days
            if age_days < 14:
                return {
                    "should_run": False,
                    "reason": f"First listing is only {age_days} days old — need 14 days of data before optimising",
                    "extra": {"active_listings": active, "days_since_first_listing": age_days},
                }

        return {
            "should_run": True,
            "reason": f"{active} active listing(s) with sufficient history — OK to optimise",
            "extra": {"active_listings": active},
        }
    except Exception as e:
        return {"should_run": True, "reason": f"Condition check failed ({e}) — running anyway", "extra": {}}


def check_memory() -> dict:
    """Memory agent always runs — it's cheap and fast."""
    return {
        "should_run": True,
        "reason": "Memory agent always runs daily",
        "extra": {},
    }


def check_anomaly() -> dict:
    """Anomaly detector always runs — it's cheap and fast."""
    return {
        "should_run": True,
        "reason": "Anomaly detector always runs daily",
        "extra": {},
    }


def check_scout() -> dict:
    """
    Skip if:
    - Already ran this week and found unreviewed proposals
    - 3+ pending proposals already in the table
    """
    try:
        pending = _safe_count(
            supabase.table("scout_proposals")
            .select("id", count="exact")
            .eq("status", "pending")
        )
        if pending >= 3:
            return {
                "should_run": False,
                "reason": f"{pending} pending proposals already awaiting review — approve/ignore them first",
                "extra": {"pending_proposals": pending},
            }

        # Check if scout ran this week and generated proposals that haven't been reviewed
        week = _week_start()
        proposals_this_week = _safe_count(
            supabase.table("scout_proposals")
            .select("id", count="exact")
            .gte("created_at", week)
            .eq("status", "pending")
        )
        if proposals_this_week > 0:
            return {
                "should_run": False,
                "reason": f"Scout already ran this week and found {proposals_this_week} unreviewed proposal(s)",
                "extra": {"proposals_this_week": proposals_this_week},
            }

        return {
            "should_run": True,
            "reason": f"{pending} pending proposals, none from this week — OK to scout",
            "extra": {"pending_proposals": pending},
        }
    except Exception as e:
        return {"should_run": True, "reason": f"Condition check failed ({e}) — running anyway", "extra": {}}


def check_reporting() -> dict:
    """Reporting agent always runs on Sunday — it's once a week."""
    return {
        "should_run": True,
        "reason": "Reporting runs every Sunday regardless of conditions",
        "extra": {},
    }


def check_prompt_evolution() -> dict:
    """
    Skip if fewer than 20 design records exist in memory
    (not enough data to evaluate prompt patterns).
    """
    try:
        design_memory = _safe_count(
            supabase.table("memory")
            .select("id", count="exact")
            .eq("category", "design_quality")
        )
        if design_memory < 20:
            return {
                "should_run": False,
                "reason": f"Only {design_memory} design quality memory records — need 20+ before evolving prompts",
                "extra": {"design_memory_count": design_memory},
            }

        return {
            "should_run": True,
            "reason": f"{design_memory} design quality records available — enough data to evolve prompts",
            "extra": {"design_memory_count": design_memory},
        }
    except Exception as e:
        return {"should_run": True, "reason": f"Condition check failed ({e}) — running anyway", "extra": {}}


# ─── All conditions at once (for the dashboard endpoint) ──────────────────────

def all_conditions() -> dict:
    """
    Return the current run-readiness of every agent.
    Called by GET /scheduler/conditions in dashboard/server.py.
    """
    return {
        "research":         check_research(),
        "design":           check_design(),
        "publisher":        check_publisher(),
        "fiverr_orders":    check_fiverr_orders(),
        "performance":      check_performance(),
        "memory":           check_memory(),
        "anomaly":          check_anomaly(),
        "scout":            check_scout(),
        "reporting":        check_reporting(),
        "prompt_evolution": check_prompt_evolution(),
        "_checked_at":      datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(all_conditions(), indent=2))
