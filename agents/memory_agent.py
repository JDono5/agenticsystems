"""
agents/memory_agent.py — Daily memory writer (spec Section 9.1)

Runs daily at 10PM (America/Chicago) via scheduler/main.py.
Writes three observation types to the memory table:
  1. QA pass rate per variation angle (last 7 days, min 3 samples)
  2. Avg net profit per listing per niche (all-time)
  3. Agent reliability / run rate (last 7 days)

All observations written via memory_client.remember().
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

from core.supabase_client import supabase
from core.memory_client import remember
from publishers.fiverr_learning import get_niche_memory

AGENT_NAME = "memory_agent"

# Scheduled daily agents we want to track reliability for
SCHEDULED_DAILY_AGENTS = [
    "research_agent",
    "design_agent",
    "reporting_agent",
    "memory_agent",
    "anomaly_detector",
]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ─── 1. QA pass rate per variation angle ─────────────────────────────────────

def _calc_qa_pass_rates() -> list[dict]:
    """
    For each variation angle seen in the last 7 days, calculate pass rate.
    Minimum 3 samples required before writing to memory.
    Returns list of {angle, pass_rate, approved, total} dicts.
    """
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=7))
    try:
        rows = (
            supabase.table("designs")
            .select("variation_angle, status")
            .gte("created_at", cutoff)
            .in_("status", ["approved", "rejected"])
            .execute()
            .data
        )
    except Exception as e:
        print(f"[{AGENT_NAME}]   QA pass rate query failed (missing column?): {e}")
        return []

    buckets: dict[str, dict] = defaultdict(lambda: {"approved": 0, "total": 0})
    for row in rows:
        angle = row.get("variation_angle") or "unknown"
        buckets[angle]["total"] += 1
        if row.get("status") == "approved":
            buckets[angle]["approved"] += 1

    results = []
    for angle, counts in buckets.items():
        if counts["total"] >= 3:
            pass_rate = round(counts["approved"] / counts["total"], 3)
            results.append({
                "angle":     angle,
                "pass_rate": pass_rate,
                "approved":  counts["approved"],
                "total":     counts["total"],
            })
    return results


# ─── 2. Avg net profit per listing per niche ─────────────────────────────────

def _calc_niche_performance() -> list[dict]:
    """
    All-time: for each platform+niche combination, calculate average net profit
    per listing that has at least one sale.

    Memory key: '{platform}_{niche}_avg_net_per_listing'
    """
    try:
        listings_rows = (
            supabase.table("listings")
            .select("id, niche, platform")
            .execute()
            .data
        )
    except Exception as e:
        print(f"[{AGENT_NAME}]   Niche performance query failed: {e}")
        return []
    if not listings_rows:
        return []

    listing_map = {r["id"]: r for r in listings_rows}

    # Get all sales
    sales_rows = (
        supabase.table("sales")
        .select("listing_id, net_profit")
        .execute()
        .data
    )

    # Aggregate: platform_niche -> {total_net, listing_ids}
    agg: dict[str, dict] = defaultdict(lambda: {"total_net": 0.0, "listing_ids": set()})
    for sale in sales_rows:
        lid = sale.get("listing_id")
        if not lid or lid not in listing_map:
            continue
        listing = listing_map[lid]
        platform = listing.get("platform", "etsy")
        niche    = listing.get("niche",    "unknown")
        key      = f"{platform}_{niche}"
        agg[key]["total_net"] += float(sale.get("net_profit") or 0)
        agg[key]["listing_ids"].add(lid)

    results = []
    for key, data in agg.items():
        listing_count = len(data["listing_ids"])
        if listing_count > 0:
            avg_net = round(data["total_net"] / listing_count, 2)
            results.append({
                "key":            f"{key}_avg_net_per_listing",
                "avg_net":        avg_net,
                "listing_count":  listing_count,
            })
    return results


# ─── 3. Agent reliability (run rate last 7 days) ──────────────────────────────

def _calc_agent_reliability() -> list[dict]:
    """
    For each scheduled daily agent, calculate how many of the last 7 days
    it appeared in cost_log. Reliability = days_seen / 7.

    Agents that haven't run at all yet get reliability 0 but are still
    recorded so the anomaly_detector can track them.
    """
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=7))
    rows = (
        supabase.table("cost_log")
        .select("agent, timestamp")
        .gte("timestamp", cutoff)
        .execute()
        .data
    )

    # Collect distinct dates per agent
    agent_dates: dict[str, set] = defaultdict(set)
    last_seen_map: dict[str, str] = {}
    for row in rows:
        agent = row["agent"]
        ts    = row["timestamp"]
        day   = ts[:10]  # YYYY-MM-DD
        agent_dates[agent].add(day)
        if agent not in last_seen_map or ts > last_seen_map[agent]:
            last_seen_map[agent] = ts

    results = []
    for agent in SCHEDULED_DAILY_AGENTS:
        days_seen    = len(agent_dates.get(agent, set()))
        reliability  = round(days_seen / 7, 3)
        last_seen    = last_seen_map.get(agent, None)
        results.append({
            "agent":       agent,
            "days_seen":   days_seen,
            "reliability": reliability,
            "last_seen":   last_seen,
        })
    return results


# ─── 4. Fiverr performance per niche ─────────────────────────────────────────

# Known Fiverr niches the pipeline handles
FIVERR_NICHES = ["gaming", "finance", "fitness", "food", "tech", "lifestyle", "education"]


def _calc_fiverr_performance(prior_total: int) -> int:
    """
    Read Fiverr review and order memory, calculate per-niche avg ratings, and
    identify prompt patterns that correlate with high vs. low reviews.

    Memory keys written:
      - fiverr_{niche}_avg_rating   (category: niche_performance)
      - fiverr_best_prompt_patterns  (category: prompt_performance)
      - fiverr_worst_prompt_patterns (category: prompt_performance)
      - fiverr_overall_avg_rating    (category: niche_performance)

    Returns number of records written.
    """
    written = 0
    niche_ratings:    dict[str, list[float]] = defaultdict(list)
    high_patterns:    list[str] = []
    low_patterns:     list[str] = []

    for niche in FIVERR_NICHES:
        try:
            niche_data = get_niche_memory(niche)
        except Exception as e:
            print(f"[{AGENT_NAME}]   Fiverr niche memory read failed ({niche}): {e}")
            continue

        if not niche_data:
            continue

        # Collect ratings from order history stored under platform_health
        orders = niche_data.get("orders", [])
        for order_record in orders:
            rating = order_record.get("rating")
            if rating is not None:
                niche_ratings[niche].append(float(rating))
                prompt_used = order_record.get("prompt_used", "")[:200]
                if float(rating) >= 5.0 and prompt_used:
                    high_patterns.append(prompt_used)
                elif float(rating) < 4.0 and prompt_used:
                    low_patterns.append(prompt_used)

        if niche_ratings[niche]:
            avg = round(sum(niche_ratings[niche]) / len(niche_ratings[niche]), 2)
            sample = len(niche_ratings[niche])
            remember(
                category="niche_performance",
                key=f"fiverr_{niche}_avg_rating",
                value={
                    "avg_rating":  avg,
                    "order_count": sample,
                    "niche":       niche,
                    "platform":    "fiverr",
                },
                confidence=min(sample / 5, 1.0),
                sample_size=sample,
            )
            print(f"[{AGENT_NAME}]   Fiverr '{niche}' avg rating: {avg:.1f} ({sample} orders)")
            written += 1

    # Overall Fiverr avg rating (all niches combined)
    all_ratings = [r for ratings in niche_ratings.values() for r in ratings]
    if all_ratings:
        overall_avg = round(sum(all_ratings) / len(all_ratings), 2)
        remember(
            category="niche_performance",
            key="fiverr_overall_avg_rating",
            value={
                "avg_rating":  overall_avg,
                "order_count": len(all_ratings),
                "platform":    "fiverr",
            },
            confidence=min(len(all_ratings) / 10, 1.0),
            sample_size=len(all_ratings),
        )
        print(f"[{AGENT_NAME}]   Fiverr overall avg rating: {overall_avg:.1f} ({len(all_ratings)} orders)")
        written += 1

    # Best prompt patterns (from 5-star reviews)
    if high_patterns:
        remember(
            category="prompt_performance",
            key="fiverr_best_prompt_patterns",
            value={
                "patterns":     high_patterns[:10],
                "count":        len(high_patterns),
                "source":       "5-star Fiverr reviews",
            },
            confidence=min(len(high_patterns) / 5, 1.0),
            sample_size=len(high_patterns),
        )
        print(f"[{AGENT_NAME}]   Fiverr best patterns: {len(high_patterns)} high-performing prompts")
        written += 1
    else:
        print(f"[{AGENT_NAME}]   Fiverr best patterns: no 5-star data yet")

    # Worst prompt patterns (from sub-4-star reviews)
    if low_patterns:
        remember(
            category="prompt_performance",
            key="fiverr_worst_prompt_patterns",
            value={
                "patterns":  low_patterns[:10],
                "count":     len(low_patterns),
                "source":    "sub-4-star Fiverr reviews",
            },
            confidence=min(len(low_patterns) / 5, 1.0),
            sample_size=len(low_patterns),
        )
        print(f"[{AGENT_NAME}]   Fiverr worst patterns: {len(low_patterns)} underperforming prompts")
        written += 1
    else:
        print(f"[{AGENT_NAME}]   Fiverr worst patterns: no sub-4-star data yet")

    if written == 0:
        print(f"[{AGENT_NAME}]   Fiverr performance: no review data yet")

    return written


# ─── Entry point ──────────────────────────────────────────────────────────────

def run() -> None:
    print(
        f"[{AGENT_NAME}] --- Starting "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---"
    )

    total_written = 0

    # --- 1. QA pass rates ---
    pass_rates = _calc_qa_pass_rates()
    if pass_rates:
        for item in pass_rates:
            angle = item["angle"]
            remember(
                category="design_quality",
                key=f"pass_rate_{angle}",
                value={
                    "pass_rate":  item["pass_rate"],
                    "approved":   item["approved"],
                    "total":      item["total"],
                    "window_days": 7,
                },
                confidence=min(item["total"] / 10, 1.0),
                sample_size=item["total"],
            )
            print(f"[{AGENT_NAME}]   QA pass rate '{angle}': {item['pass_rate']:.1%} ({item['approved']}/{item['total']})")
            total_written += 1
    else:
        print(f"[{AGENT_NAME}]   QA pass rates: no angles with >= 3 samples yet")

    # --- 2. Niche performance ---
    niche_perf = _calc_niche_performance()
    if niche_perf:
        for item in niche_perf:
            remember(
                category="niche_performance",
                key=item["key"],
                value={
                    "avg_net_per_listing": item["avg_net"],
                    "listing_count":       item["listing_count"],
                },
                confidence=min(item["listing_count"] / 5, 1.0),
                sample_size=item["listing_count"],
            )
            print(f"[{AGENT_NAME}]   Niche '{item['key']}': avg net ${item['avg_net']:.2f} over {item['listing_count']} listing(s)")
            total_written += 1
    else:
        print(f"[{AGENT_NAME}]   Niche performance: no sales data yet")

    # --- 3. Agent reliability ---
    reliability = _calc_agent_reliability()
    for item in reliability:
        remember(
            category="agent_reliability",
            key=f"{item['agent']}_error_rate",
            value={
                "reliability":  item["reliability"],
                "days_seen":    item["days_seen"],
                "last_seen":    item["last_seen"],
                "window_days":  7,
            },
            confidence=0.9,
            sample_size=item["days_seen"],
        )
        status = "ok" if item["reliability"] >= 0.7 else "degraded" if item["reliability"] > 0 else "no data"
        print(f"[{AGENT_NAME}]   Agent '{item['agent']}': {item['days_seen']}/7 days ({status})")
        total_written += 1

    # --- 4. Fiverr performance by niche ---
    fiverr_written = _calc_fiverr_performance(total_written)
    total_written += fiverr_written

    print(f"[{AGENT_NAME}] --- Done: {total_written} memory record(s) written ---")


if __name__ == "__main__":
    run()
