"""
scheduler/main.py — APScheduler entry point (spec Section 10)

All times are America/Chicago (Lubbock TX timezone).
Run with: python scheduler/main.py
Deploy command (Railway Procfile): worker: python scheduler/main.py
"""

import os
import sys
import logging

# Project root on path so agents/ and core/ are importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR

from agents.research_agent       import run as run_research
from agents.design_agent         import run as run_design
from agents.publisher_agent      import run as run_publisher
from agents.performance_agent    import run as run_performance
from agents.reporting_agent      import run as run_reporting
from agents.scout_agent          import run as run_scout
from agents.memory_agent         import run as run_memory
from agents.anomaly_detector     import run as run_anomaly
from agents.prompt_evolution_agent import run as run_prompt_evolution

from publishers.fiverr           import check_fiverr_orders, fulfill_order, check_for_reviews
from publishers.fiverr_scout     import scout_fiverr_opportunities as run_fiverr_scout
from core.spend_monitor          import check_cap
from core.emailer                import send_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

scheduler = BlockingScheduler(timezone="America/Chicago")


def guarded(fn, name, *args):
    """
    Wrap an agent run function with:
      - Spend-cap check (skips if monthly cap is hit)
      - Exception catch + email alert on failure
      - Structured logging
    """
    def wrapper():
        if not check_cap():
            logging.warning(f"[{name}] skipped — spend cap hit")
            return
        try:
            result = fn(*args) if args else fn()
            logging.info(f"[{name}] complete: {result}")
        except Exception as e:
            send_alert(f"Agent failed: {name}", str(e))
            logging.error(f"[{name}] ERROR: {e}")
    return wrapper


def fiverr_check():
    """Poll IMAP for new Fiverr orders and fulfill each one."""
    orders = check_fiverr_orders()
    for order in orders:
        fulfill_order(order)


def fiverr_reviews():
    """Poll IMAP for Fiverr review notification emails and log to memory."""
    check_for_reviews()


# ── Error listener ────────────────────────────────────────────────────────────
scheduler.add_listener(
    lambda e: send_alert("Scheduler error", str(e.exception)),
    EVENT_JOB_ERROR,
)

# ── Daily pipeline ─────────────────────────────────────────────────────────────
scheduler.add_job(guarded(run_research,   "research_etsy",   "etsy"),   "cron", hour=6,  minute=0)
scheduler.add_job(guarded(run_research,   "research_fiverr", "fiverr"), "cron", hour=6,  minute=30)
scheduler.add_job(guarded(run_design,     "design_agent",    "etsy"),   "cron", hour=8,  minute=0)
scheduler.add_job(guarded(run_publisher,  "publisher_agent", "etsy"),   "cron", hour=10, minute=0)
scheduler.add_job(guarded(fiverr_check,   "fiverr_check"),              "cron", hour="*/4", minute=0)
scheduler.add_job(guarded(run_memory,     "memory_agent"),              "cron", hour=22, minute=0)
scheduler.add_job(guarded(run_anomaly,    "anomaly_detector"),          "cron", hour=23, minute=0)

# ── Twice-weekly ──────────────────────────────────────────────────────────────
scheduler.add_job(
    guarded(run_performance, "performance_agent", "etsy"),
    "cron", day_of_week="tue,thu", hour=11, minute=0,
)

# ── Daily review check ────────────────────────────────────────────────────────
scheduler.add_job(guarded(fiverr_reviews, "fiverr_reviews"), "cron", hour=20, minute=0)

# ── Weekly (Sunday) ───────────────────────────────────────────────────────────
scheduler.add_job(guarded(run_scout,            "scout_agent"),       "cron", day_of_week="sun", hour=7,  minute=0)
scheduler.add_job(guarded(run_fiverr_scout,     "fiverr_scout"),      "cron", day_of_week="sun", hour=7,  minute=15)
scheduler.add_job(guarded(run_prompt_evolution, "prompt_evo"),        "cron", day_of_week="sun", hour=7,  minute=30)
scheduler.add_job(guarded(run_reporting,        "reporting_agent"),   "cron", day_of_week="sun", hour=9,  minute=0)


if __name__ == "__main__":
    logging.info("[scheduler] Autonomous Income Engine starting — America/Chicago timezone")
    logging.info("[scheduler] Jobs registered:")
    for job in scheduler.get_jobs():
        logging.info(f"  {job.id} → next run: {job.next_run_time}")
    scheduler.start()
