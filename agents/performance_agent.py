"""
agents/performance_agent.py — Listing optimization (spec Section 6.6)

Runs Tuesday and Thursday 11AM (America/Chicago) via scheduler/main.py.

For each Etsy listing live > 14 days:
  1. Fetch stats (impressions, clicks) — MOCKED until Etsy account is live
  2. Update impressions + clicks in Supabase
  3. Evaluate thresholds and rewrite/pause/flag as needed
  4. Write niche performance observations to memory

To swap in the real Etsy stats call:
  Replace _fetch_listing_stats_mock() with _fetch_listing_stats_live()
  and set USE_MOCK_ETSY_STATS = False.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

import anthropic
import requests

from core.supabase_client import supabase
from core.cost_logger import log_cost, calc_anthropic_cost
from core.spend_monitor import check_cap
from core.error_handler import api_call_with_retry
from core.memory_client import remember
from core.job_queue import enqueue

AGENT_NAME  = "performance_agent"
CLAUDE_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# ── Toggle this to False when the Etsy seller account is live ──────────────────
USE_MOCK_ETSY_STATS = True

IMPRESSIONS_LOW_THRESHOLD = 50    # after 14 days → rewrite title+tags
CTR_LOW_THRESHOLD         = 0.05  # 5% CTR → flag for owner
IMPRESSIONS_DEAD_THRESHOLD = 10   # after 30 days → pause listing


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ─── Etsy stats fetch (mock) ──────────────────────────────────────────────────

def _fetch_listing_stats_mock(listing_id: str) -> dict:
    """
    Returns zeroed-out stats until the Etsy account is live.
    Shape mirrors the real Etsy stats API response.
    """
    return {"impressions": 0, "clicks": 0}


def _fetch_listing_stats_live(listing_id: str) -> dict:
    """
    Real Etsy stats call (swap in when account is live).
    GET https://openapi.etsy.com/v3/application/shops/{shop_id}/listings/{listing_id}/stats
    """
    shop_id      = os.getenv("ETSY_SHOP_ID", "")
    access_token = os.getenv("ETSY_ACCESS_TOKEN", "")
    api_key      = os.getenv("ETSY_API_KEY", "")

    resp = requests.get(
        f"https://openapi.etsy.com/v3/application/shops/{shop_id}/listings/{listing_id}/stats",
        headers={
            "Authorization": f"Bearer {access_token}",
            "x-api-key":     api_key,
        },
        timeout=15,
    )
    if resp.status_code == 401:
        # Trigger token refresh (publishers/etsy_printify.py handles the full refresh flow)
        raise PermissionError("Etsy access token expired — refresh required")
    resp.raise_for_status()
    data = resp.json()
    return {
        "impressions": data.get("views", 0),
        "clicks":      data.get("clicks", 0),
    }


def _fetch_stats(listing_id: str) -> dict:
    if USE_MOCK_ETSY_STATS:
        return _fetch_listing_stats_mock(listing_id)
    return _fetch_listing_stats_live(listing_id)


# ─── Listing copy optimizer ───────────────────────────────────────────────────

OPTIMIZE_PROMPT = """\
You are an Etsy SEO specialist. A listing has low impressions after being live for 14+ days.
Rewrite its title and tags to improve search visibility.

Current listing:
Title: {title}
Tags: {tags}
Niche: {niche}
Occupation: {occupation}

Rules:
- Title: max 140 chars, front-load the top 2-3 buyer-intent keywords, keep the funny/human element
- Tags: exactly 13 tags, mix of specific (e.g. "electrician dad mug") and broad ("funny gift mug")
- Do NOT change the product or design concept — only improve discoverability
- Avoid keyword stuffing — tags should read naturally

Return ONLY valid JSON, no markdown:
{{"title": "new title here", "tags": ["tag1", "tag2", ..., "tag13"]}}"""


def _rewrite_listing(listing: dict) -> dict | None:
    """
    Ask Claude to rewrite the title and tags for a low-impression listing.
    Returns {title, tags} or None on failure.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Extract occupation from niche (e.g. "electrician gifts" → "electrician")
    niche      = listing.get("niche", "")
    occupation = niche.replace(" gifts", "").replace(" gift", "").strip()
    tags_raw   = listing.get("tags", [])
    tags_str   = ", ".join(tags_raw) if isinstance(tags_raw, list) else str(tags_raw)

    prompt = OPTIMIZE_PROMPT.format(
        title=listing.get("title", ""),
        tags=tags_str,
        niche=niche,
        occupation=occupation,
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    input_t  = response.usage.input_tokens
    output_t = response.usage.output_tokens
    cost     = calc_anthropic_cost(CLAUDE_MODEL, input_t, output_t)
    log_cost(agent=AGENT_NAME, provider="anthropic", model=CLAUDE_MODEL,
             tokens_used=input_t + output_t, cost_usd=cost)

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.rstrip())

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[{AGENT_NAME}]   Rewrite parse failed for listing {listing['id']}")
        return None

    # Validate
    title = data.get("title", "")
    tags  = data.get("tags", [])
    if len(title) > 140:
        title = title[:140]
    if len(tags) != 13:
        print(f"[{AGENT_NAME}]   Tag count wrong ({len(tags)}) — skipping rewrite")
        return None

    return {"title": title, "tags": tags}


# ─── Entry point ──────────────────────────────────────────────────────────────

def run(platform: str = "etsy") -> dict:
    """
    Evaluate all active listings that have been live for > 14 days.
    Returns {"optimized": int, "flagged": int, "unchanged": int}.
    """
    print(
        f"[{AGENT_NAME}] --- Starting ({platform}) "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---"
    )

    if not check_cap():
        print(f"[{AGENT_NAME}] Spend cap reached — exiting.")
        return {"optimized": 0, "flagged": 0, "unchanged": 0}

    now    = datetime.now(timezone.utc)
    cutoff_14 = _iso(now - timedelta(days=14))
    cutoff_30 = _iso(now - timedelta(days=30))

    # Listings live > 14 days
    listings = (
        supabase.table("listings")
        .select("*")
        .eq("platform", platform)
        .eq("status", "active")
        .lt("published_at", cutoff_14)
        .execute()
        .data
    )

    if not listings:
        print(f"[{AGENT_NAME}]   No listings > 14 days old yet.")
        return {"optimized": 0, "flagged": 0, "unchanged": 0}

    print(f"[{AGENT_NAME}]   Evaluating {len(listings)} listing(s)")

    optimized = 0
    flagged   = 0
    unchanged = 0
    niche_net: dict[str, list[float]] = {}

    for listing in listings:
        lid        = listing["id"]
        title      = listing.get("title", "")[:60]
        niche      = listing.get("niche", "unknown")
        pub_at     = listing.get("published_at", "")
        is_old_30  = pub_at and pub_at < cutoff_30

        # --- Step 1: fetch stats ---
        stats = api_call_with_retry(
            lambda l=lid: _fetch_stats(l),
            max_retries=3,
            agent_name=AGENT_NAME,
        ) or {"impressions": 0, "clicks": 0}

        impressions = stats.get("impressions", 0)
        clicks      = stats.get("clicks", 0)

        # --- Step 2: update Supabase ---
        supabase.table("listings").update({
            "impressions":        impressions,
            "clicks":             clicks,
            "last_optimized_at":  _iso(now),
        }).eq("id", lid).execute()

        # --- Step 3: evaluate ---
        ctr = (clicks / impressions) if impressions > 0 else 0.0

        if is_old_30 and impressions < IMPRESSIONS_DEAD_THRESHOLD:
            # Pause dead listings
            supabase.table("listings").update({"status": "paused"}).eq("id", lid).execute()
            print(f"[{AGENT_NAME}]   PAUSED '{title}' — {impressions} impressions after 30 days")
            optimized += 1

        elif impressions < IMPRESSIONS_LOW_THRESHOLD:
            # Rewrite title + tags
            print(f"[{AGENT_NAME}]   Low impressions ({impressions}) — rewriting '{title}'")
            new_copy = api_call_with_retry(
                lambda l=listing: _rewrite_listing(l),
                max_retries=2,
                agent_name=AGENT_NAME,
            )
            if new_copy:
                supabase.table("listings").update({
                    "title":             new_copy["title"],
                    "tags":              new_copy["tags"],
                    "last_optimized_at": _iso(now),
                }).eq("id", lid).execute()
                print(f"[{AGENT_NAME}]   Rewrote to: '{new_copy['title'][:60]}'")
                optimized += 1
            else:
                unchanged += 1

        elif impressions >= IMPRESSIONS_LOW_THRESHOLD and ctr < CTR_LOW_THRESHOLD:
            # Good impressions, poor CTR — flag for owner review
            print(f"[{AGENT_NAME}]   Low CTR {ctr:.1%} ({impressions} impressions) — flagging '{title}'")
            try:
                enqueue("optimize_listing", platform=platform, payload={
                    "listing_id": lid,
                    "reason":     "low_ctr",
                    "impressions": impressions,
                    "clicks":      clicks,
                    "ctr":         round(ctr, 4),
                })
            except Exception:
                pass
            flagged += 1

        else:
            unchanged += 1

        # --- Step 4: record niche net profit for memory ---
        # Approximate: we don't have per-listing net here, use impression/click signals
        if niche not in niche_net:
            niche_net[niche] = []
        niche_net[niche].append(impressions)

    # Write niche performance signals to memory
    for niche, impression_list in niche_net.items():
        avg_impressions = round(sum(impression_list) / len(impression_list), 1)
        remember(
            category="niche_performance",
            key=f"{platform}_{niche}_avg_impressions",
            value={
                "avg_impressions": avg_impressions,
                "listing_count":   len(impression_list),
                "platform":        platform,
            },
            confidence=min(len(impression_list) / 5, 1.0),
            sample_size=len(impression_list),
        )

    print(
        f"[{AGENT_NAME}] --- Done: "
        f"{optimized} optimized, {flagged} flagged, {unchanged} unchanged ---"
    )
    return {"optimized": optimized, "flagged": flagged, "unchanged": unchanged}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", default="etsy")
    args = parser.parse_args()
    run(platform=args.platform)
