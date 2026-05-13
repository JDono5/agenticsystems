"""
core/memory_client.py — High-level helpers for the memory table (spec Section 5.3)

Wraps the raw upsert_memory / get_memory functions in supabase_client with
domain-specific convenience methods used by memory_agent, performance_agent,
prompt_evolution_agent, etc.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from core.supabase_client import (
    upsert_memory,
    get_memory,
    get_memory_by_category,
)


def remember(
    category: str,
    key: str,
    value: dict,
    confidence: float = 0.5,
    sample_size: int = 1,
) -> None:
    """
    Upsert a memory record. Key must be unique across all categories.

    category choices: design_quality | niche_performance | platform_health |
                      agent_reliability | prompt_performance | scout_findings
    """
    upsert_memory(
        category=category,
        key=key,
        value=value,
        confidence=confidence,
        sample_size=sample_size,
    )


def recall(key: str) -> dict | None:
    """
    Return the full memory row for a key, or None if not found.
    Row contains: id, category, key, value (dict), confidence, sample_size, last_updated
    """
    return get_memory(key)


def recall_category(category: str) -> list[dict]:
    """Return all memory rows for a given category."""
    return get_memory_by_category(category)


def recall_best_niches(platform: str, top_n: int = 5) -> list[str]:
    """
    Return the top N niche slugs for a platform, ranked by avg_net_per_listing
    stored in memory by memory_agent.

    Memory key format: '{platform}_{niche}_avg_net_per_listing'
    Value format: {"avg_net_per_listing": float, "listing_count": int}
    """
    rows = recall_category("niche_performance")
    prefix = f"{platform}_"
    suffix = "_avg_net_per_listing"

    candidates = []
    for row in rows:
        key = row.get("key", "")
        if key.startswith(prefix) and key.endswith(suffix):
            niche = key[len(prefix):-len(suffix)]
            avg   = float(row.get("value", {}).get("avg_net_per_listing", 0.0))
            candidates.append((niche, avg))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [niche for niche, _ in candidates[:top_n]]


def recall_best_design_angles(platform: str) -> list[str]:
    """
    Return variation angles sorted by QA pass rate (highest first).

    Memory key format: 'pass_rate_{angle}'
    Value format: {"pass_rate": float, "sample_size": int}
    """
    rows = recall_category("design_quality")
    angles = []
    for row in rows:
        key = row.get("key", "")
        if key.startswith("pass_rate_"):
            angle     = key[len("pass_rate_"):]
            pass_rate = float(row.get("value", {}).get("pass_rate", 0.5))
            angles.append((angle, pass_rate))

    angles.sort(key=lambda x: x[1], reverse=True)
    return [angle for angle, _ in angles]


def recall_prompt_pass_rate(variation_angle: str) -> float:
    """
    Return the rolling 7-day QA pass rate for a given variation angle.
    Returns 0.5 (neutral) if no data exists.
    """
    row = recall(f"pass_rate_{variation_angle}")
    if not row:
        return 0.5
    return float(row.get("value", {}).get("pass_rate", 0.5))
