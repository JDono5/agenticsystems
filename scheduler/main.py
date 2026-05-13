"""
scheduler/main.py — State-aware APScheduler entry point (spec Section 10)

All times are America/Chicago (Lubbock TX timezone).
Run with: python scheduler/main.py

Each agent is guarded by two layers:
  1. Spend-cap check (global hard stop)
  2. Per-agent condition check (smart skip with logged reason)
"""

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR

from agents.research_agent         import run as run_research
from agents.design_agent           import run as run_design
from agents.publisher_agent        import run as run_publisher
from agents.performance_agent      import run as run_performance
from agents.reporting_agent        import run as run_reporting
from agents.scout_agent            import run as run_scout
from agents.memory_agent           import run as run_memory
from agents.anomaly_detector       import run as run_anomaly
from agents.prompt_evolution_agent import run as run_prompt_evolution

from publishers.fiverr         import check_fiverr_orders, fulfill_order, check_for_reviews
from publishers.fiverr_scout   import scout_fiverr_opportunities as run_fiverr_scout

from core.spend_monitor         import check_cap
from core.emailer               import send_alert
from core.activity_logger       import log_activity
import core.scheduler_conditions as conditions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

scheduler = BlockingScheduler(timezone="America/Chicago")


# ─── Core wrapper ─────────────────────────────────────────────────────────────

def guarded(fn, name: str, condition_fn=None, *args):
    """
    Wrap an agent run function with:
      1. Monthly spend-cap check — skips entire scheduler if cap is hit
      2. Per-agent condition check — smart skip with logged reason
      3. Exception catch + email alert on failure
      4. activity_log entry on start, success, and error
    """
    def wrapper():
        # ── 1. Global spend cap ────────────────────────────────────────────────
        if not check_cap():
            msg = f"{name} skipped — monthly spend cap hit"
            logging.warning(f"[{name}] {msg}")
            log_activity(name, "system", msg)
            return

        # ── 2. Per-agent condition ─────────────────────────────────────────────
        if condition_fn is not None:
            try:
                cond = condition_fn()
            except Exception as ce:
                cond = {"should_run": True, "reason": f"condition check errored: {ce}"}

            if not cond.get("should_run", True):
                reason = cond.get("reason", "condition not met")
                logging.info(f"[{name}] skipped — {reason}")
                log_activity(name, "system", f"Skipped: {reason}")
                return

        # ── 3. Run with error handling ─────────────────────────────────────────
        log_activity(name, "system", f"{name} started")
        try:
            result = fn(*args) if args else fn()
            logging.info(f"[{name}] complete: {result}")
            log_activity(name, "system", f"{name} completed: {result}")
        except Exception as e:
            send_alert(f"Agent failed: {name}", str(e))
            logging.error(f"[{name}] ERROR: {e}")
            log_activity(name, "error", f"{name} failed: {e}")

    return wrapper


# ─── Fiverr order check (special: always runs if IMAP configured) ─────────────

def fiverr_check():
    """Poll IMAP for new Fiverr orders and fulfill each one."""
    cond = conditions.check_fiverr_orders()
    if not cond["should_run"]:
        logging.info(f"[fiverr_check] skipped — {cond['reason']}")
        return

    try:
        orders = check_fiverr_orders()
        for order in orders:
            if order:
                log_activity("fiverr_fulfillment", "order_received",
                             f"Order received: {order.get('order_id', 'unknown')}")
                fulfill_order(order)
    except Exception as e:
        send_alert("Fiverr order check failed", str(e))
        logging.error(f"[fiverr_check] ERROR: {e}")
        log_activity("fiverr_fulfillment", "error", f"Order check failed: {e}")


def fiverr_reviews():
    """Poll IMAP for Fiverr review notification emails and log to memory."""
    cond = conditions.check_fiverr_orders()  # same IMAP dependency
    if not cond["should_run"]:
        return
    try:
        check_for_reviews()
    except Exception as e:
        logging.error(f"[fiverr_reviews] ERROR: {e}")


# ─── Error listener ───────────────────────────────────────────────────────────

def _on_scheduler_error(event):
    send_alert("Scheduler error", str(event.exception))
    log_activity("scheduler", "error", f"Scheduler error: {event.exception}")

scheduler.add_listener(_on_scheduler_error, EVENT_JOB_ERROR)


# ─── Daily pipeline ───────────────────────────────────────────────────────────

scheduler.add_job(
    guarded(run_research, "research_etsy",   conditions.check_research, "etsy"),
    "cron", hour=6, minute=0,
)
scheduler.add_job(
    guarded(run_research, "research_fiverr", conditions.check_research, "fiverr"),
    "cron", hour=6, minute=30,
)
scheduler.add_job(
    guarded(run_design, "design_agent", conditions.check_design, "etsy"),
    "cron", hour=8, minute=0,
)
scheduler.add_job(
    guarded(run_publisher, "publisher_agent", conditions.check_publisher, "etsy"),
    "cron", hour=10, minute=0,
)
scheduler.add_job(
    fiverr_check,
    "cron", hour="*/4", minute=0,
)
scheduler.add_job(
    guarded(run_memory, "memory_agent", conditions.check_memory),
    "cron", hour=22, minute=0,
)
scheduler.add_job(
    guarded(run_anomaly, "anomaly_detector", conditions.check_anomaly),
    "cron", hour=23, minute=0,
)

# ─── Twice-weekly ─────────────────────────────────────────────────────────────

scheduler.add_job(
    guarded(run_performance, "performance_agent", conditions.check_performance, "etsy"),
    "cron", day_of_week="tue,thu", hour=11, minute=0,
)

# ─── Daily review check ───────────────────────────────────────────────────────

scheduler.add_job(fiverr_reviews, "cron", hour=20, minute=0)

# ─── Weekly (Sunday) ──────────────────────────────────────────────────────────

scheduler.add_job(
    guarded(run_scout, "scout_agent", conditions.check_scout),
    "cron", day_of_week="sun", hour=7, minute=0,
)
scheduler.add_job(
    guarded(run_fiverr_scout, "fiverr_scout", conditions.check_scout),
    "cron", day_of_week="sun", hour=7, minute=15,
)
scheduler.add_job(
    guarded(run_prompt_evolution, "prompt_evolution", conditions.check_prompt_evolution),
    "cron", day_of_week="sun", hour=7, minute=30,
)
scheduler.add_job(
    guarded(run_reporting, "reporting_agent", conditions.check_reporting),
    "cron", day_of_week="sun", hour=9, minute=0,
)


if __name__ == "__main__":
    log_activity("scheduler", "system", "Autonomous Income Engine starting")
    logging.info("[scheduler] Autonomous Income Engine starting — America/Chicago timezone")
    logging.info("[scheduler] Jobs registered:")
    for job in scheduler.get_jobs():
        logging.info(f"  {job.id} -> next run: {job.next_run_time}")
    scheduler.start()
