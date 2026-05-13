"""
agents/orchestrator_agent.py — Master agent / natural language interface (spec Section 7)

Used by dashboard/server.py /orchestrator endpoint.
Not scheduled — invoked on every owner message through the dashboard chat.
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

import anthropic

from core.supabase_client import supabase, get_pending_proposals
from core.cost_logger import log_cost, calc_anthropic_cost
from core.finance import get_weekly_pnl, get_all_time_pnl
from core.memory_client import recall

AGENT_NAME = "orchestrator"
MODEL      = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")


# ─── Exact system prompt from spec Section 7.1 ────────────────────────────────
# Uses simple string replacement (not .format) so JSON curly braces never clash.

ORCHESTRATOR_SYSTEM_TEMPLATE = """\
You are the orchestrator of an autonomous income engine. You manage AI agents \
running income streams: ACTIVE_STREAMS_PLACEHOLDER.

Personality: direct, competent, concise. Give best answer immediately. No \
clarifying questions unless genuinely necessary. No padding. Act like a trusted \
business partner who knows the numbers.

Current system state:
SYSTEM_STATE_PLACEHOLDER

What you can do without approval:
- Answer questions using injected state data
- Task workers: trigger research, design runs, optimization
- Diagnose problems and propose fixes
- Adjust config: niches, spend cap, draft mode

What requires owner approval:
- Launching a new income stream type never run before
- Spending above monthly cap
- Deleting live listings or products

What only the owner can do (tell them exactly what is needed):
- Signing up for a new platform account
- Providing a payment method to a new platform
- Providing a new API key after signup

When a credential is needed, respond exactly:
'To launch [stream] I need [credential name]. Here is exactly how to get it:
1. [exact step]
2. [exact step]
Paste the key here and I will continue setup automatically.'

End action responses with: 'Done. I will report results in the weekly summary \
unless something needs your attention sooner.'"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ─── State helpers ─────────────────────────────────────────────────────────────

def _get_active_stream_names() -> list[str]:
    """Return platforms with active listings, or any platform with a brief this week."""
    streams: set[str] = set()
    try:
        rows = (
            supabase.table("listings")
            .select("platform")
            .eq("status", "active")
            .execute()
            .data
        )
        for r in rows:
            if r.get("platform"):
                streams.add(r["platform"])
    except Exception:
        pass

    if not streams:
        week_ago = _iso(datetime.now(timezone.utc) - timedelta(days=7))
        try:
            rows = (
                supabase.table("research_briefs")
                .select("platform")
                .gte("created_at", week_ago)
                .execute()
                .data
            )
            for r in rows:
                if r.get("platform"):
                    streams.add(r["platform"])
        except Exception:
            pass

    return sorted(streams) if streams else ["etsy (no listings yet)"]


def _count_listings_by_status(status: str) -> int:
    try:
        result = (
            supabase.table("listings")
            .select("id", count="exact")
            .eq("status", status)
            .execute()
        )
        return result.count or 0
    except Exception:
        return 0


def _count_designs_approved_today() -> int:
    try:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result = (
            supabase.table("designs")
            .select("id", count="exact")
            .eq("status", "approved")
            .gte("created_at", _iso(today_start))
            .execute()
        )
        return result.count or 0
    except Exception:
        return 0


def _get_top_listing_this_week(week_start: str, now_iso: str) -> dict | None:
    """Return {title, revenue} for the highest-grossing listing this week, or None."""
    try:
        sales = (
            supabase.table("sales")
            .select("listing_id, gross_revenue")
            .gte("order_date", week_start)
            .lt("order_date", now_iso)
            .execute()
            .data
        )
        if not sales:
            return None

        by_listing: dict[str, float] = {}
        for s in sales:
            lid = s.get("listing_id")
            if lid:
                by_listing[lid] = by_listing.get(lid, 0.0) + float(s["gross_revenue"])

        if not by_listing:
            return None

        top_lid, top_rev = max(by_listing.items(), key=lambda x: x[1])
        listing = (
            supabase.table("listings")
            .select("title")
            .eq("id", top_lid)
            .limit(1)
            .execute()
            .data
        )
        title = listing[0]["title"] if listing else top_lid
        return {"title": title, "gross_revenue": round(top_rev, 2)}
    except Exception:
        return None


def _get_recent_agent_errors(hours: int = 48) -> list[str]:
    """
    Return a short list of recent error signals:
    - Agents that missed runs (from job_queue self_healing entries)
    """
    errors: list[str] = []
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(hours=hours))
    try:
        jobs = (
            supabase.table("job_queue")
            .select("payload, created_at")
            .eq("job_type", "self_healing")
            .gte("created_at", cutoff)
            .execute()
            .data
        )
        for j in jobs:
            payload = j.get("payload", {})
            agent   = payload.get("agent", "unknown")
            reason  = payload.get("reason", "missed_run")
            errors.append(f"{agent}: {reason}")
    except Exception:
        pass
    return errors


def _count_fiverr_orders_this_week(week_start: str, now_iso: str) -> int:
    """Count fulfilled Fiverr orders from cost_log this week (agent='fiverr_fulfillment')."""
    try:
        result = (
            supabase.table("cost_log")
            .select("id", count="exact")
            .eq("agent", "fiverr_fulfillment")
            .gte("timestamp", week_start)
            .lt("timestamp", now_iso)
            .execute()
        )
        return result.count or 0
    except Exception:
        return 0


def _get_fiverr_avg_rating() -> float | None:
    """Read overall Fiverr avg rating from memory (written by memory_agent)."""
    try:
        row = recall("fiverr_overall_avg_rating")
        if row and isinstance(row.get("value"), dict):
            return row["value"].get("avg_rating")
    except Exception:
        pass
    return None


def _get_fiverr_revenue_this_week(week_start: str, now_iso: str) -> float:
    """Sum net_profit from sales table where platform='fiverr' this week."""
    try:
        rows = (
            supabase.table("sales")
            .select("net_profit")
            .eq("platform", "fiverr")
            .gte("order_date", week_start)
            .lt("order_date", now_iso)
            .execute()
            .data
        )
        return round(sum(float(r.get("net_profit") or 0) for r in rows), 2)
    except Exception:
        return 0.0


def _get_monthly_spend() -> float:
    try:
        now   = datetime.now(timezone.utc)
        start = _iso(datetime(now.year, now.month, 1, tzinfo=timezone.utc))
        rows  = (
            supabase.table("cost_log")
            .select("cost_usd")
            .gte("timestamp", start)
            .execute()
            .data
        )
        return round(sum(float(r["cost_usd"]) for r in rows), 4)
    except Exception:
        return 0.0


# ─── Build live system state ───────────────────────────────────────────────────

def build_system_state() -> dict:
    """
    Called fresh before every orchestrator API call (spec Section 7.1).
    Pulls live data from Supabase and finance.py.
    """
    now   = datetime.now(timezone.utc)
    # Current week: Monday 00:00 UTC → now
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    active_streams = _get_active_stream_names()

    week_pnl = get_weekly_pnl(_iso(monday), _iso(now))

    all_time = get_all_time_pnl()

    return {
        "timestamp":              now.isoformat(),
        "active_streams":         active_streams,
        "weekly_revenue":         week_pnl["gross_revenue"],
        "weekly_net":             week_pnl["net_profit"],
        "weekly_order_count":     week_pnl["order_count"],
        "all_time_revenue":       all_time["gross_revenue"],
        "all_time_net":           all_time["net_profit"],
        "all_time_costs":         all_time["total_costs"],
        "setup_costs_recovered":  all_time["setup_costs_recovered"],
        "remaining_to_recover":   all_time["remaining_to_recover"],
        "days_since_launch":      all_time["days_since_launch"],
        "active_listing_count":   _count_listings_by_status("active"),
        "monthly_spend":          _get_monthly_spend(),
        "monthly_cap":            float(os.getenv("MONTHLY_SPEND_CAP", "100")),
        "pending_proposals":      len(get_pending_proposals()),
        "recent_errors":          _get_recent_agent_errors(hours=48),
        "top_listing_this_week":  _get_top_listing_this_week(_iso(monday), _iso(now)),
        "designs_approved_today":    _count_designs_approved_today(),
        "draft_mode":                os.getenv("DRAFT_MODE", "true"),
        "fiverr_orders_this_week":   _count_fiverr_orders_this_week(_iso(monday), _iso(now)),
        "fiverr_avg_rating":         _get_fiverr_avg_rating(),
        "fiverr_revenue_this_week":  _get_fiverr_revenue_this_week(_iso(monday), _iso(now)),
    }


# ─── Main chat function ────────────────────────────────────────────────────────

def chat(message: str, history: list[dict]) -> dict:
    """
    Process one owner message.

    Args:
        message: The new owner message.
        history: Full prior conversation as [{role, content}, ...].

    Returns:
        {"reply": str, "history": list[dict], "cost": float}
    """
    active_streams = _get_active_stream_names()
    system_state   = build_system_state()

    # Use string replacement, not .format(), so JSON curly braces never clash
    system_prompt = ORCHESTRATOR_SYSTEM_TEMPLATE.replace(
        "ACTIVE_STREAMS_PLACEHOLDER",
        ", ".join(active_streams),
    ).replace(
        "SYSTEM_STATE_PLACEHOLDER",
        json.dumps(system_state, indent=2, default=str),
    )

    messages = history + [{"role": "user", "content": message}]

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system_prompt,
        messages=messages,
    )

    reply    = response.content[0].text
    input_t  = response.usage.input_tokens
    output_t = response.usage.output_tokens
    cost     = calc_anthropic_cost(MODEL, input_t, output_t)

    log_cost(
        agent=AGENT_NAME,
        provider="anthropic",
        model=MODEL,
        tokens_used=input_t + output_t,
        cost_usd=cost,
    )

    return {
        "reply":   reply,
        "history": messages + [{"role": "assistant", "content": reply}],
        "cost":    round(cost, 6),
    }


if __name__ == "__main__":
    # Quick CLI test
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("message", nargs="?", default="What is our current financial status?")
    args = parser.parse_args()

    print(f"You: {args.message}\n")
    result = chat(args.message, [])
    print(f"Orchestrator: {result['reply']}")
    print(f"\n(cost: ${result['cost']:.6f})")
