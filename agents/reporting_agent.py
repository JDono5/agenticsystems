"""
agents/reporting_agent.py — Weekly P&L email (spec Section 6.7)

Runs Sunday 9AM (America/Chicago) via scheduler/main.py.
Can also be run manually: python agents/reporting_agent.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

from core.finance import (
    get_weekly_pnl,
    get_all_time_pnl,
    get_cost_breakdown,
)
from core.supabase_client import (
    supabase,
    get_pending_proposals,
)
from core.emailer import send_report
from core.error_handler import api_call_with_retry

AGENT_NAME = "reporting_agent"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _week_range() -> tuple[str, str]:
    """Return ISO strings for Mon 00:00 → Sun 23:59:59 of the most recent week."""
    now   = datetime.now(timezone.utc)
    # Last Sunday = today if Sunday, else previous Sunday
    days_since_sunday = (now.weekday() + 1) % 7
    sunday  = now - timedelta(days=days_since_sunday)
    monday  = sunday - timedelta(days=6)
    start   = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end     = sunday.replace(hour=23, minute=59, second=59, microsecond=999999)
    return _iso(start), _iso(end)


def _count_designs_this_week(start: str, end: str) -> dict:
    gen = (
        supabase.table("designs").select("id", count="exact")
        .gte("created_at", start).lt("created_at", end)
        .execute()
    )
    approved = (
        supabase.table("designs").select("id", count="exact")
        .gte("created_at", start).lt("created_at", end)
        .eq("status", "approved")
        .execute()
    )
    return {"generated": gen.count or 0, "approved": approved.count or 0}


def _count_listings_published_this_week(start: str, end: str) -> int:
    result = (
        supabase.table("listings").select("id", count="exact")
        .gte("published_at", start).lt("published_at", end)
        .execute()
    )
    return result.count or 0


def _count_active_listings() -> int:
    result = supabase.table("listings").select("id", count="exact").eq("status", "active").execute()
    return result.count or 0


def _count_briefs_this_week(start: str, end: str) -> int:
    result = (
        supabase.table("research_briefs").select("id", count="exact")
        .gte("created_at", start).lt("created_at", end)
        .execute()
    )
    return result.count or 0


def _get_top_listings(start: str, end: str, n: int = 3) -> list[dict]:
    """Return the top N listings by gross revenue this week."""
    sales = (
        supabase.table("sales")
        .select("listing_id, gross_revenue")
        .gte("order_date", start)
        .lt("order_date", end)
        .execute()
        .data
    )
    if not sales:
        return []

    # Aggregate by listing_id
    by_listing: dict = {}
    for row in sales:
        lid = row.get("listing_id")
        if lid:
            by_listing[lid] = by_listing.get(lid, 0.0) + float(row["gross_revenue"])

    top = sorted(by_listing.items(), key=lambda x: x[1], reverse=True)[:n]

    result = []
    for lid, revenue in top:
        listing_row = supabase.table("listings").select("title, niche").eq("id", lid).limit(1).execute().data
        title = listing_row[0]["title"] if listing_row else lid
        result.append({"title": title, "revenue": revenue})
    return result


# ─── Email body builder ───────────────────────────────────────────────────────

def _build_email_body(
    week_start: str,
    week_end:   str,
    pnl:        dict,
    all_time:   dict,
    breakdown:  dict,
    designs:    dict,
    listings_published: int,
    active_listings:    int,
    briefs_created:     int,
    top_performers:     list[dict],
    pending_proposals:  int,
) -> str:

    start_fmt = week_start[:10]
    end_fmt   = week_end[:10]

    net   = pnl["net_profit"]
    gross = pnl["gross_revenue"]

    # Recovery status
    if all_time["setup_costs_recovered"]:
        recovery_line = "All setup costs recovered. System is net positive all-time."
    else:
        remaining = all_time["remaining_to_recover"]
        recovery_line = f"${remaining:.2f} remaining to recover initial setup costs."

    # Top performers section
    if top_performers:
        performers_lines = "\n".join(
            f"  {i+1}. {p['title'][:60]}  — ${p['revenue']:.2f}"
            for i, p in enumerate(top_performers)
        )
    else:
        performers_lines = "  No sales this week yet."

    # Alerts placeholder (anomaly_detector will populate this in a later build)
    alerts_section = "  No alerts this week."

    body = f"""\
================================================================
 WEEKLY INCOME ENGINE REPORT
 {start_fmt} to {end_fmt}
================================================================

REVENUE
  Gross sales:        ${gross:.2f}
  Orders:             {pnl['order_count']}
  Active listings:    {active_listings}

COSTS
  API costs:          ${pnl['api_costs']:.4f}
  Fulfillment:        ${pnl['fulfillment_costs']:.2f}
  Platform fees:      ${pnl['platform_fees']:.2f}
  Other:              ${pnl['expense_costs']:.2f}
  Total costs:        ${pnl['total_costs']:.4f}

NET PROFIT / MARGIN
  Net this week:      ${net:+.2f}
  Net margin:         {pnl['net_margin_pct']:+.1f}%

ALL-TIME
  Total revenue:      ${all_time['gross_revenue']:.2f}
  Total costs:        ${all_time['total_costs']:.4f}
  Net profit:         ${all_time['net_profit']:+.2f}
  Days running:       {all_time['days_since_launch']}
  {recovery_line}

TOP PERFORMERS THIS WEEK
{performers_lines}

PIPELINE ACTIVITY
  Designs generated:  {designs['generated']}
  Designs approved:   {designs['approved']}
  Listings published: {listings_published}
  Research briefs:    {briefs_created}

ALERTS
{alerts_section}

SCOUT PROPOSALS
  {pending_proposals} pending proposal(s) awaiting review in dashboard.

================================================================
 Autonomous Income Engine — automated report
================================================================
"""
    return body


def _build_html_email(plain_body: str) -> str:
    """Wrap plain-text report in minimal HTML for send_report()."""
    escaped = plain_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines   = escaped.split("\n")
    html_lines = []
    for line in lines:
        if line.startswith("==="):
            html_lines.append(f'<hr style="border:1px solid #333;">')
        elif line.strip() and not line.startswith(" "):
            html_lines.append(f'<strong>{line}</strong><br>')
        else:
            html_lines.append(f'{line}<br>')

    html = (
        '<html><body style="font-family:monospace;font-size:13px;'
        'background:#0d1117;color:#e6edf3;padding:20px;">\n'
        + "\n".join(html_lines)
        + "\n</body></html>"
    )
    return html


# ─── Entry point ──────────────────────────────────────────────────────────────

def run() -> None:
    """
    Build and send the weekly P&L email.

    Subject: Weekly Report - {date_range} | Net: ${net_profit}
    Runs Sunday 9AM — triggered by scheduler/main.py.
    """
    print(f"[{AGENT_NAME}] Building weekly report...")

    week_start, week_end = _week_range()
    print(f"[{AGENT_NAME}]   Period: {week_start[:10]} to {week_end[:10]}")

    pnl      = get_weekly_pnl(week_start, week_end)
    all_time = get_all_time_pnl()
    breakdown = get_cost_breakdown(week_start, week_end)

    designs            = _count_designs_this_week(week_start, week_end)
    listings_published = _count_listings_published_this_week(week_start, week_end)
    active_listings    = _count_active_listings()
    briefs_created     = _count_briefs_this_week(week_start, week_end)
    top_performers     = _get_top_listings(week_start, week_end)
    pending_proposals  = len(get_pending_proposals())

    body_plain = _build_email_body(
        week_start=week_start,
        week_end=week_end,
        pnl=pnl,
        all_time=all_time,
        breakdown=breakdown,
        designs=designs,
        listings_published=listings_published,
        active_listings=active_listings,
        briefs_created=briefs_created,
        top_performers=top_performers,
        pending_proposals=pending_proposals,
    )

    net    = pnl["net_profit"]
    start  = week_start[:10]
    end    = week_end[:10]
    subject = f"Weekly Report - {start} to {end} | Net: ${net:+.2f}"

    html_body = _build_html_email(body_plain)

    print(f"[{AGENT_NAME}]   Sending: {subject}")
    send_report(subject=subject, html_body=html_body)
    print(f"[{AGENT_NAME}]   Done.")


if __name__ == "__main__":
    run()
