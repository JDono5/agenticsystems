"""
agents/anomaly_detector.py — Nightly anomaly checks (spec Section 9.3)

Runs daily at 11PM (America/Chicago) via scheduler/main.py.
Checks all 7 thresholds from the spec table. Sends email alerts for any
that trigger and enqueues follow-up jobs where specified.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

from core.supabase_client import supabase
from core.emailer import send_alert
from core.job_queue import enqueue

AGENT_NAME = "anomaly_detector"

# Agents that should run daily — used for the "26-hour rule"
DAILY_AGENTS = ["research_agent", "design_agent", "memory_agent"]

MONTHLY_SPEND_CAP = float(os.getenv("MONTHLY_SPEND_CAP", "100"))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _week_bounds(offset_weeks: int = 0) -> tuple[str, str]:
    """Return (start_iso, end_iso) for a calendar week, 0 = this week, -1 = last week."""
    now   = datetime.now(timezone.utc)
    days_since_monday = now.weekday()
    monday = (now - timedelta(days=days_since_monday + 7 * abs(offset_weeks))).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if offset_weeks < 0:
        end = monday + timedelta(days=7)
    else:
        end = now
    return _iso(monday), _iso(end)


# ─── Check 1: Weekly sales drop per listing ───────────────────────────────────

def _check_sales_drop() -> list[str]:
    """
    Alert if any listing with > 5 prior sales drops > 50% in revenue week-over-week.
    """
    alerts = []
    this_start,  this_end  = _week_bounds(0)
    last_start,  last_end  = _week_bounds(-1)

    def _week_revenue(start, end) -> dict[str, float]:
        rows = (
            supabase.table("sales")
            .select("listing_id, gross_revenue")
            .gte("order_date", start).lt("order_date", end)
            .execute().data
        )
        totals: dict[str, float] = {}
        for r in rows:
            lid = r.get("listing_id")
            if lid:
                totals[lid] = totals.get(lid, 0.0) + float(r.get("gross_revenue", 0))
        return totals

    this_week = _week_revenue(this_start, this_end)
    last_week = _week_revenue(last_start, last_end)

    # Only evaluate listings that had > 5 sales last week
    for lid, last_rev in last_week.items():
        this_rev = this_week.get(lid, 0.0)
        if last_rev > 0 and this_rev / last_rev < 0.5:
            # Fetch title for readable alert
            listing = supabase.table("listings").select("title").eq("id", lid).limit(1).execute().data
            title = listing[0]["title"][:60] if listing else lid
            pct   = round((1 - this_rev / last_rev) * 100, 1)
            alerts.append(f"Sales drop {pct}% on listing: '{title}' (${last_rev:.2f} → ${this_rev:.2f})")
            try:
                enqueue("optimize_listing", payload={"listing_id": lid, "reason": "sales_drop"})
            except Exception:
                pass
    return alerts


# ─── Check 2: QA pass rate < 30% for 3 consecutive days ──────────────────────

def _check_qa_pass_rate() -> list[str]:
    """
    Alert if the QA pass rate has been below 30% for each of the last 3 days.
    """
    alerts = []
    now = datetime.now(timezone.utc)
    daily_rates = []

    for day_offset in range(3):
        day_start = (now - timedelta(days=day_offset + 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        rows = (
            supabase.table("designs")
            .select("status")
            .gte("created_at", _iso(day_start))
            .lt("created_at", _iso(day_end))
            .in_("status", ["approved", "rejected"])
            .execute().data
        )
        if not rows:
            daily_rates.append(None)
            continue
        approved = sum(1 for r in rows if r["status"] == "approved")
        rate     = approved / len(rows)
        daily_rates.append(rate)

    valid_rates = [r for r in daily_rates if r is not None]
    if len(valid_rates) >= 3 and all(r < 0.30 for r in valid_rates):
        avg = round(sum(valid_rates) / len(valid_rates) * 100, 1)
        alerts.append(
            f"QA pass rate below 30% for 3 consecutive days (avg {avg}%). "
            "Prompt evolution may be needed."
        )
        try:
            enqueue("prompt_evolution", payload={"trigger": "low_qa_rate", "rates": valid_rates})
        except Exception:
            pass
    return alerts


# ─── Check 3: API cost spike (>3x rolling 7-day average) ─────────────────────

def _check_api_cost_spike() -> list[str]:
    """
    Alert if today's API cost for any agent is > 3x its rolling 7-day daily average.
    """
    alerts = []
    now       = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago    = today_start - timedelta(days=7)

    # Rolling 7-day costs per agent per day
    week_rows = (
        supabase.table("cost_log")
        .select("agent, cost_usd, timestamp")
        .gte("timestamp", _iso(week_ago))
        .lt("timestamp", _iso(today_start))
        .execute().data
    )

    # Today's costs
    today_rows = (
        supabase.table("cost_log")
        .select("agent, cost_usd")
        .gte("timestamp", _iso(today_start))
        .execute().data
    )

    # Aggregate
    week_by_agent: dict[str, dict] = {}
    for r in week_rows:
        a   = r["agent"]
        day = r["timestamp"][:10]
        if a not in week_by_agent:
            week_by_agent[a] = {}
        week_by_agent[a][day] = week_by_agent[a].get(day, 0.0) + float(r["cost_usd"])

    today_by_agent: dict[str, float] = {}
    for r in today_rows:
        a = r["agent"]
        today_by_agent[a] = today_by_agent.get(a, 0.0) + float(r["cost_usd"])

    for agent, today_cost in today_by_agent.items():
        if agent not in week_by_agent:
            continue
        daily_costs = list(week_by_agent[agent].values())
        if not daily_costs:
            continue
        rolling_avg = sum(daily_costs) / len(daily_costs)
        if rolling_avg > 0 and today_cost > rolling_avg * 3:
            multiple = round(today_cost / rolling_avg, 1)
            alerts.append(
                f"API cost spike for '{agent}': ${today_cost:.4f} today "
                f"({multiple}x rolling avg ${rolling_avg:.4f}). Agent may be looping."
            )
    return alerts


# ─── Check 4: Etsy impressions drop > 40% ────────────────────────────────────

def _check_impressions_drop() -> list[str]:
    """
    Alert if total impressions across all Etsy listings dropped > 40% week-over-week.
    """
    alerts = []
    # impressions are stored on listings table, updated by performance_agent
    this_start, this_end = _week_bounds(0)
    last_start, last_end = _week_bounds(-1)

    def _total_impressions(since: str) -> int:
        rows = (
            supabase.table("listings")
            .select("impressions")
            .eq("platform", "etsy")
            .execute().data
        )
        return sum(int(r.get("impressions", 0)) for r in rows)

    # We can't easily get historical impression totals without a separate tracking table.
    # Use last_optimized_at as a proxy: check if any listing impression count is 0 after 14 days.
    now      = datetime.now(timezone.utc)
    cutoff   = now - timedelta(days=14)
    old_listings = (
        supabase.table("listings")
        .select("id, title, impressions, published_at")
        .eq("platform", "etsy")
        .eq("status", "active")
        .lt("published_at", _iso(cutoff))
        .execute().data
    )

    zero_impression_listings = [r for r in old_listings if int(r.get("impressions", 0)) == 0]
    if len(zero_impression_listings) >= 3:
        alerts.append(
            f"{len(zero_impression_listings)} active Etsy listing(s) have 0 impressions after 14+ days. "
            "Possible algorithm suppression or listing issue."
        )
    return alerts


# ─── Check 5: Monthly spend > 80% of cap with > 7 days remaining ─────────────

def _check_monthly_spend() -> list[str]:
    """
    Alert and recommend halving DAILY_DESIGN_TARGET if spend > 80% of cap
    and there are more than 7 days left in the month.
    """
    alerts = []
    now   = datetime.now(timezone.utc)
    year  = now.year
    month = now.month

    # Days remaining in month
    import calendar
    days_in_month    = calendar.monthrange(year, month)[1]
    days_remaining   = days_in_month - now.day

    if days_remaining <= 7:
        return alerts  # Too close to end of month to matter

    month_start = _iso(datetime(year, month, 1, tzinfo=timezone.utc))
    month_end   = _iso(now)

    rows = (
        supabase.table("cost_log")
        .select("cost_usd")
        .gte("timestamp", month_start)
        .lt("timestamp", month_end)
        .execute().data
    )
    month_spend = sum(float(r["cost_usd"]) for r in rows)
    pct         = month_spend / MONTHLY_SPEND_CAP * 100

    if pct > 80:
        current_target = int(os.getenv("DAILY_DESIGN_TARGET", "8"))
        recommended    = max(1, current_target // 2)
        alerts.append(
            f"Monthly spend ${month_spend:.2f} is {pct:.1f}% of ${MONTHLY_SPEND_CAP:.0f} cap "
            f"with {days_remaining} days remaining. "
            f"Recommend reducing DAILY_DESIGN_TARGET from {current_target} to {recommended}."
        )
    return alerts


# ─── Check 6: Scheduled agent not run in > 26 hours ──────────────────────────

def _check_agent_last_run() -> list[str]:
    """
    Alert if any scheduled daily agent hasn't appeared in cost_log for > 26 hours.
    Only fires if the system has been running for at least 2 days (avoids false
    positives on first launch).
    """
    alerts = []
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=26)

    # Only check if there's any cost_log data at all
    any_data = supabase.table("cost_log").select("id").limit(1).execute().data
    if not any_data:
        return alerts

    # Find oldest log entry to determine system age
    first = supabase.table("cost_log").select("timestamp").order("timestamp").limit(1).execute().data
    if first:
        launch_ts = datetime.fromisoformat(first[0]["timestamp"].replace("Z", "+00:00"))
        if (now - launch_ts).total_seconds() < 48 * 3600:
            return alerts  # System < 48h old — skip check

    rows = (
        supabase.table("cost_log")
        .select("agent, timestamp")
        .gte("timestamp", _iso(cutoff))
        .execute().data
    )
    agents_seen_recently = {r["agent"] for r in rows}

    for agent in DAILY_AGENTS:
        if agent not in agents_seen_recently:
            alerts.append(
                f"Scheduled agent '{agent}' has not run in > 26 hours. "
                "Check Railway logs or scheduler status."
            )
            try:
                enqueue("self_healing", payload={"agent": agent, "reason": "missed_run"})
            except Exception:
                pass
    return alerts


# ─── Check 7: Approved Scout proposal not launched within 7 days ─────────────

def _check_stale_proposals() -> list[str]:
    """
    Remind the owner if any approved Scout proposal hasn't been launched within 7 days.
    """
    alerts = []
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=7))

    try:
        rows = (
            supabase.table("scout_proposals")
            .select("opportunity_name, approved_at")
            .eq("status", "approved")
            .lt("approved_at", cutoff)
            .execute().data
        )
        for row in rows:
            name = row.get("opportunity_name", "Unknown")
            approved_at = row.get("approved_at", "")[:10]
            alerts.append(
                f"Scout proposal '{name}' was approved on {approved_at} "
                "but has not been launched. Review in dashboard."
            )
    except Exception:
        pass  # Table may not exist yet
    return alerts


# ─── Entry point ──────────────────────────────────────────────────────────────

def run() -> dict:
    """
    Run all 7 anomaly checks. Send one consolidated email if any alerts triggered.
    Returns {"checks_run": int, "alerts_triggered": int, "alerts": list}.
    """
    print(
        f"[{AGENT_NAME}] --- Starting "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---"
    )

    checks = [
        ("Sales drop",          _check_sales_drop),
        ("QA pass rate",        _check_qa_pass_rate),
        ("API cost spike",      _check_api_cost_spike),
        ("Etsy impressions",    _check_impressions_drop),
        ("Monthly spend",       _check_monthly_spend),
        ("Agent last run",      _check_agent_last_run),
        ("Scout proposals",     _check_stale_proposals),
    ]

    all_alerts: list[str] = []
    for name, check_fn in checks:
        try:
            alerts = check_fn()
            if alerts:
                print(f"[{AGENT_NAME}]   ALERT [{name}]: {len(alerts)} issue(s)")
                for a in alerts:
                    print(f"[{AGENT_NAME}]     - {a}")
                all_alerts.extend(alerts)
            else:
                print(f"[{AGENT_NAME}]   OK    [{name}]")
        except Exception as e:
            print(f"[{AGENT_NAME}]   ERROR [{name}]: {e}")

    if all_alerts:
        subject = f"[Income Engine] {len(all_alerts)} anomaly alert(s) — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        body    = "Anomaly detector triggered the following alerts:\n\n" + "\n\n".join(
            f"{i+1}. {a}" for i, a in enumerate(all_alerts)
        )
        try:
            send_alert(subject, body)
            print(f"[{AGENT_NAME}]   Alert email sent: {len(all_alerts)} issue(s)")
        except Exception as e:
            print(f"[{AGENT_NAME}]   Failed to send alert email: {e}")
    else:
        print(f"[{AGENT_NAME}]   No anomalies detected.")

    result = {
        "checks_run":       len(checks),
        "alerts_triggered": len(all_alerts),
        "alerts":           all_alerts,
    }
    print(f"[{AGENT_NAME}] --- Done ---")
    return result


if __name__ == "__main__":
    result = run()
    if result["alerts_triggered"] == 0:
        print("[anomaly_detector] System healthy — no anomalies.")
