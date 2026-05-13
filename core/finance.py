"""
core/finance.py — Unified P&L and expense tracking (spec Section 5.2)

All monetary values are USD. Dates/times are UTC ISO strings unless noted.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

from core.supabase_client import (
    supabase,
    save_expense,
    get_expenses_in_range,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _sum_sales(start_iso: str, end_iso: str, platform: str = None) -> dict:
    q = (
        supabase.table("sales")
        .select("gross_revenue, net_profit")
        .gte("order_date", start_iso)
        .lt("order_date", end_iso)
    )
    if platform:
        q = q.eq("platform", platform)
    rows = q.execute().data
    return {
        "gross_revenue": sum(float(r["gross_revenue"]) for r in rows),
        "net_from_sales": sum(float(r["net_profit"]) for r in rows),
        "order_count":   len(rows),
    }


def _sum_api_costs(start_iso: str, end_iso: str) -> float:
    rows = (
        supabase.table("cost_log")
        .select("cost_usd")
        .gte("timestamp", start_iso)
        .lt("timestamp", end_iso)
        .execute()
        .data
    )
    return sum(float(r["cost_usd"]) for r in rows)


def _sum_api_costs_by_agent(start_iso: str, end_iso: str) -> dict:
    rows = (
        supabase.table("cost_log")
        .select("agent, cost_usd")
        .gte("timestamp", start_iso)
        .lt("timestamp", end_iso)
        .execute()
        .data
    )
    by_agent: dict = {}
    for r in rows:
        a = r["agent"]
        by_agent[a] = round(by_agent.get(a, 0.0) + float(r["cost_usd"]), 6)
    return by_agent


def _sum_expenses(start_iso: str, end_iso: str) -> dict:
    rows = get_expenses_in_range(start_iso, end_iso)
    totals: dict = {"fulfillment": 0.0, "platform_fee": 0.0, "other": 0.0, "total": 0.0}
    for r in rows:
        cat = r.get("category", "other")
        amt = float(r.get("amount_usd", 0))
        if cat == "fulfillment":
            totals["fulfillment"] += amt
        elif cat == "platform_fee":
            totals["platform_fee"] += amt
        else:
            totals["other"] += amt
        totals["total"] += amt
    return totals


def _count_active_listings() -> int:
    result = supabase.table("listings").select("id", count="exact").eq("status", "active").execute()
    return result.count or 0


# ─── Public API ───────────────────────────────────────────────────────────────

def get_weekly_pnl(week_start: str, week_end: str) -> dict:
    """
    Returns P&L for the given date range (ISO strings, UTC).

    Keys: gross_revenue, api_costs, fulfillment_costs, platform_fees,
          expense_costs, total_costs, net_profit, net_margin_pct, order_count
    """
    sales    = _sum_sales(week_start, week_end)
    api_cost = _sum_api_costs(week_start, week_end)
    expenses = _sum_expenses(week_start, week_end)

    gross          = sales["gross_revenue"]
    fulfillment    = expenses["fulfillment"]
    platform_fees  = expenses["platform_fee"]
    other_expenses = expenses["other"]
    total_costs    = api_cost + fulfillment + platform_fees + other_expenses

    net_profit     = gross - total_costs
    net_margin_pct = round((net_profit / gross * 100) if gross else 0.0, 1)

    return {
        "gross_revenue":     round(gross, 2),
        "api_costs":         round(api_cost, 4),
        "fulfillment_costs": round(fulfillment, 2),
        "platform_fees":     round(platform_fees, 2),
        "expense_costs":     round(other_expenses, 2),
        "total_costs":       round(total_costs, 4),
        "net_profit":        round(net_profit, 2),
        "net_margin_pct":    net_margin_pct,
        "order_count":       sales["order_count"],
    }


def get_monthly_pnl(year: int, month: int) -> dict:
    """Same structure as get_weekly_pnl but for a full calendar month."""
    from datetime import date
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return get_weekly_pnl(_iso(start), _iso(end))


def get_all_time_pnl() -> dict:
    """
    Lifetime P&L from epoch to now.

    Extra keys:
      setup_costs_recovered: bool  — True once all-time net_profit > 0
      remaining_to_recover: float  — dollars remaining until breakeven (0 if recovered)
      days_since_launch: int
    """
    epoch = "2020-01-01T00:00:00+00:00"
    now   = _iso(datetime.now(timezone.utc))
    pnl   = get_weekly_pnl(epoch, now)

    # Rough launch date — first cost_log row
    first = supabase.table("cost_log").select("timestamp").order("timestamp").limit(1).execute().data
    if first:
        launch_dt  = datetime.fromisoformat(first[0]["timestamp"].replace("Z", "+00:00"))
        days_since = (datetime.now(timezone.utc) - launch_dt).days
    else:
        days_since = 0

    net = pnl["net_profit"]
    pnl.update({
        "setup_costs_recovered": net >= 0,
        "remaining_to_recover":  round(abs(net), 2) if net < 0 else 0.0,
        "days_since_launch":     days_since,
    })
    return pnl


def get_cost_breakdown(start: str, end: str) -> dict:
    """
    Returns:
      api_costs_by_agent: {agent_name: cost}
      fulfillment_total: float
      platform_fees_total: float
      other_expenses: float
      grand_total: float
    """
    by_agent      = _sum_api_costs_by_agent(start, end)
    expenses      = _sum_expenses(start, end)
    api_total     = sum(by_agent.values())
    grand_total   = api_total + expenses["total"]

    return {
        "api_costs_by_agent":  by_agent,
        "api_total":           round(api_total, 4),
        "fulfillment_total":   round(expenses["fulfillment"], 2),
        "platform_fees_total": round(expenses["platform_fee"], 2),
        "other_expenses":      round(expenses["other"], 2),
        "grand_total":         round(grand_total, 4),
    }


def log_fulfillment_cost(order_id: str, listing_id: str, amount: float) -> None:
    """Log a Printify/fulfillment charge to the expenses table."""
    today = datetime.now(timezone.utc).date().isoformat()
    save_expense(
        expense_date=today,
        category="fulfillment",
        platform="printify",
        description=f"Fulfillment for order {order_id} / listing {listing_id}",
        amount_usd=amount,
        recurring=False,
    )


def log_platform_fee(platform: str, description: str, amount: float) -> None:
    """Log an Etsy / Fiverr / other platform fee to the expenses table."""
    today = datetime.now(timezone.utc).date().isoformat()
    save_expense(
        expense_date=today,
        category="platform_fee",
        platform=platform,
        description=description,
        amount_usd=amount,
        recurring=False,
    )


def format_weekly_report_section(pnl: dict) -> str:
    """
    Return a formatted plain-text block for the weekly P&L email section.

    Matches the spec email format from Section 6.7.
    """
    net    = pnl["net_profit"]
    gross  = pnl["gross_revenue"]
    margin = pnl["net_margin_pct"]

    profit_line = (
        f"  Net profit:         ${net:+.2f}  ({margin:+.1f}% margin)"
    )

    if net < 0:
        footer = f"  Still in ramp-up. Break-even at approximately 4-6 sales/month."
    else:
        footer = f"  Profitable. System running well."

    lines = [
        "── FINANCIAL SUMMARY ──────────────────────────────",
        f"  Gross revenue:      ${gross:.2f}   ({pnl['order_count']} order(s))",
        f"  API costs:          ${pnl['api_costs']:.4f}",
        f"  Fulfillment costs:  ${pnl['fulfillment_costs']:.2f}",
        f"  Platform fees:      ${pnl['platform_fees']:.2f}",
        f"  Other expenses:     ${pnl['expense_costs']:.2f}",
        f"  Total costs:        ${pnl['total_costs']:.4f}",
        profit_line,
        footer,
    ]
    return "\n".join(lines)
