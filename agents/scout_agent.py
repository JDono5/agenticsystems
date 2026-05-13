"""
agents/scout_agent.py — Weekly opportunity finder (spec Section 8)

Inherits the shared orchestration loop (scrape → evaluate → save) from
BaseScout (core/pipeline_base.py).

EtsyScout adds:
  - 4-source scraping: Etsy trending, Fiverr categories, Google Trends, Reddit
  - Active-streams context injection into the Claude prompt
  - Post-save job enqueueing so the orchestrator knows proposals arrived

Runs Sunday 7AM (America/Chicago) via scheduler/main.py.
Set MOCK_SCOUT=true to use embedded mock data (no network calls).
"""

import os
import re
import sys
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv

from core.pipeline_base import BaseScout
from core.supabase_client import supabase, save_proposal
from core.error_handler import api_call_with_retry
from core.job_queue import enqueue

load_dotenv()

AGENT_NAME = "scout_agent"
MOCK_SCOUT = os.getenv("MOCK_SCOUT", "false").lower() == "true"
MAX_PROPOSALS = 3

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ─── Spec Section 8.3 evaluation prompt ──────────────────────────────────────

SCOUT_PROMPT = """\
You are evaluating new income stream opportunities for an autonomous agent \
system currently running: {active_streams}.

The system can fulfill: AI-generated graphic designs, text content, \
thumbnails, automated marketplace publishing.

Research data collected this week:
{research_data}

Previously rejected opportunities (do not re-propose):
{rejected_history}

Identify up to 3 opportunities meeting ALL criteria:
1. Fulfillable by existing agent output or a simple new publisher module
2. Zero inventory or fulfillment risk
3. Free to launch (under $50 setup)
4. Realistic $200+/month at steady state within 60 days
5. First order or revenue possible within 30 days

For each return JSON:
{{
  "opportunity_name": "",
  "platform": "",
  "how_it_works": "2-3 sentences",
  "agent_needed": "existing_with_config | new_publisher_module | new_agent",
  "setup_time_hours": 0,
  "monthly_potential_usd": 0,
  "risk_description": "one sentence",
  "credential_required": true,
  "credential_instructions": "exact signup steps or null"
}}

Return JSON array only. Empty array if nothing qualifies. Do not re-propose \
anything already running or in the rejected list.\
"""


# ─── Mock data ────────────────────────────────────────────────────────────────

_MOCK_ETSY = """\
Etsy Trending (market/trending snapshot):
- Custom vinyl sticker sheets (holographic, clear) — consistently top 10, avg $8-15
- Digital Canva templates (social media, business planner, wedding) — fast growing
- AI-generated art prints (instant digital download, 8x10/12x16) — new category, low comp
- Personalized occupation mugs — evergreen, high search volume
- YouTube thumbnail Canva template bundles — strong rising demand from creators
- Teacher appreciation SVG bundles — seasonal peaks, year-round tail
- Custom pet portraits (digital download) — high average sale price $25-60
- Wedding digital invitation suites — premium pricing, high conversion"""

_MOCK_FIVERR = """\
Fiverr Category Browse (active listings snapshot):
- Graphics & Design > YouTube Thumbnails — 40k+ gigs, top sellers $800-2k/month
  Growing 35% YoY. Buyers repeat monthly. AI-assisted sellers win on speed.
- AI Services > AI Art & Illustration — fastest growing category. New gigs ranking
  quickly. $15-50/image. Subscription clients common.
- Digital Marketing > Social Media Content Packages — recurring monthly buyers.
  Canva-template fulfillment works well. $50-150/month per client.
- Writing > Product Descriptions for Etsy/Amazon — high volume, AI handles well.
  $5-15/description, bulk orders common."""

_MOCK_TRENDS = """\
Google Trends US — Top Rising Topics:
- "AI art generator free" — sustained massive volume
- "passive income ideas 2026" — peak search, high buyer intent
- "print on demand business how to start" — strong upward trend
- "Etsy digital downloads ideas" — growing consistently
- "YouTube channel ideas to make money" — evergreen, rising
- "Fiverr gig ideas that make money" — buyer intent signal
- "sell SVG files Etsy" — niche but high conversion intent"""

_MOCK_REDDIT = """\
r/beermoney top posts this week:
1. "Made $847 last month selling Canva templates on Etsy — breakdown inside" (2.3k upvotes)
2. "YouTube thumbnail gig on Fiverr hit $1,200 this month — scaling tips" (1.9k)
3. "AI coloring book on Amazon KDP — Month 3 at $340/mo passive" (1.4k)
4. "Selling SVG cut files on Etsy with zero design skills — $200 first month" (1.1k)
5. "Print-on-demand mugs: Printify vs Printful comparison 2026" (876)

r/passive_income top posts this week:
1. "My POD journey Month 6: $2,400 net — what worked and what didn't" (3.1k upvotes)
2. "Digital downloads vs physical POD — full financial breakdown" (1.8k)
3. "How I automate my Etsy shop with AI tools — $4k/month update" (1.6k)
4. "YouTube thumbnail business: from $0 to $3k/mo in 90 days (Fiverr)" (1.2k)
5. "Amazon KDP AI coloring books: complete beginner guide 2026" (987)"""


# ─── Live scrapers ────────────────────────────────────────────────────────────

def _scrape_etsy_trending() -> str:
    if MOCK_SCOUT:
        print(f"[{AGENT_NAME}]   Etsy trending: using mock data (MOCK_SCOUT=true)")
        return _MOCK_ETSY
    try:
        from playwright.sync_api import sync_playwright
        print(f"[{AGENT_NAME}]   Scraping Etsy trending...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=_HTTP_HEADERS["User-Agent"])
            page.goto("https://www.etsy.com/market/trending",
                      timeout=25_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            items: list[str] = []
            for sel in [
                "[data-testid='listing-card'] h2",
                ".v2-listing-card__info h3",
                ".wt-text-body-01",
                "h3.v2-listing-card__title",
            ]:
                els = page.query_selector_all(sel)
                for el in els[:25]:
                    text = el.inner_text().strip()
                    if text and len(text) > 5:
                        items.append(f"- {text}")
                if items:
                    break
            browser.close()
        if items:
            print(f"[{AGENT_NAME}]   Etsy trending: {len(items)} items")
            return "Etsy Trending listings:\n" + "\n".join(items[:20])
    except Exception as e:
        print(f"[{AGENT_NAME}]   Etsy scrape failed ({e}) - using mock data")
    return _MOCK_ETSY


def _scrape_fiverr_categories() -> str:
    if MOCK_SCOUT:
        print(f"[{AGENT_NAME}]   Fiverr categories: using mock data (MOCK_SCOUT=true)")
        return _MOCK_FIVERR
    try:
        from playwright.sync_api import sync_playwright
        print(f"[{AGENT_NAME}]   Scraping Fiverr categories...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=_HTTP_HEADERS["User-Agent"])
            page.goto("https://www.fiverr.com/categories",
                      timeout=25_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            cats: list[str] = []
            for sel in ["[class*='category-name']", "[class*='sub-category']", "h3", "h4"]:
                els = page.query_selector_all(sel)
                for el in els[:30]:
                    text = el.inner_text().strip()
                    if text and 3 < len(text) < 80:
                        cats.append(f"- {text}")
                if len(cats) >= 10:
                    break
            browser.close()
        if cats:
            print(f"[{AGENT_NAME}]   Fiverr categories: {len(cats)} items")
            return "Fiverr Categories:\n" + "\n".join(cats[:20])
    except Exception as e:
        print(f"[{AGENT_NAME}]   Fiverr scrape failed ({e}) - using mock data")
    return _MOCK_FIVERR


def _fetch_google_trends() -> str:
    if MOCK_SCOUT:
        print(f"[{AGENT_NAME}]   Google Trends: using mock data (MOCK_SCOUT=true)")
        return _MOCK_TRENDS
    try:
        print(f"[{AGENT_NAME}]   Fetching Google Trends RSS...")
        resp = requests.get(
            "https://trends.google.com/trending/rss?geo=US",
            headers=_HTTP_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        root   = ET.fromstring(resp.content)
        topics = []
        for item in root.findall(".//item"):
            title   = item.findtext("title", "").strip()
            traffic = item.findtext(
                "{https://trends.google.com/trending/rss}approx_traffic", ""
            )
            if title:
                topics.append(f"- {title}" + (f" (~{traffic} searches)" if traffic else ""))
        if topics:
            print(f"[{AGENT_NAME}]   Google Trends: {len(topics)} topics")
            return f"Google Trends US (top {len(topics)} trending):\n" + "\n".join(topics[:20])
    except Exception as e:
        print(f"[{AGENT_NAME}]   Google Trends failed ({e}) - using mock data")
    return _MOCK_TRENDS


def _fetch_reddit() -> str:
    if MOCK_SCOUT:
        print(f"[{AGENT_NAME}]   Reddit: using mock data (MOCK_SCOUT=true)")
        return _MOCK_REDDIT
    sections: list[str] = []
    for sub in ("beermoney", "passive_income"):
        try:
            print(f"[{AGENT_NAME}]   Fetching r/{sub} top posts...")
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/top.json?t=week&limit=10",
                headers={**_HTTP_HEADERS, "Accept": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            data  = resp.json()
            posts = data.get("data", {}).get("children", [])
            lines = [f"r/{sub} top posts this week:"]
            for i, post in enumerate(posts[:10], 1):
                p     = post.get("data", {})
                title = p.get("title", "").strip()
                score = p.get("score", 0)
                if title:
                    lines.append(f'{i}. "{title}" ({score:,} upvotes)')
            if len(lines) > 1:
                sections.append("\n".join(lines))
        except Exception as e:
            print(f"[{AGENT_NAME}]   r/{sub} fetch failed ({e})")
    if sections:
        return "\n\n".join(sections)
    print(f"[{AGENT_NAME}]   Reddit: all fetches failed - using mock data")
    return _MOCK_REDDIT


# ─── Concrete scout class ─────────────────────────────────────────────────────

class EtsyScout(BaseScout):
    """
    Main opportunity scout — scans Etsy, Fiverr, Google Trends, and Reddit.

    Inherits from BaseScout:
      - run()                    — orchestration loop
      - _evaluate_with_claude()  — calls Claude, parses JSON array
      - _save_proposals()        — writes to scout_proposals with field mapping

    Adds / overrides:
      - scrape_opportunities()        — 4-source combined research string
      - _build_evaluation_prompt()    — injects active_streams context
      - _get_rejected_history()       — includes "launched" status (all platforms)
      - _on_proposals_saved()         — enqueues scout_proposal job
    """

    platform      = "etsy"
    max_proposals = MAX_PROPOSALS
    module_name   = AGENT_NAME

    def scrape_opportunities(self) -> str:
        """Collect research data from all 4 sources and return a combined string."""
        etsy   = api_call_with_retry(lambda: _scrape_etsy_trending(),    max_retries=2, agent_name=AGENT_NAME) or _MOCK_ETSY
        fiverr = api_call_with_retry(lambda: _scrape_fiverr_categories(), max_retries=2, agent_name=AGENT_NAME) or _MOCK_FIVERR
        trends = api_call_with_retry(lambda: _fetch_google_trends(),     max_retries=2, agent_name=AGENT_NAME) or _MOCK_TRENDS
        reddit = api_call_with_retry(lambda: _fetch_reddit(),            max_retries=2, agent_name=AGENT_NAME) or _MOCK_REDDIT

        return f"""
=== ETSY TRENDING ===
{etsy}

=== FIVERR CATEGORIES ===
{fiverr}

=== GOOGLE TRENDS ===
{trends}

=== REDDIT COMMUNITY SIGNALS ===
{reddit}
""".strip()

    def _build_evaluation_prompt(
        self,
        raw_data:         Any,
        rejected_history: list[str],
    ) -> str:
        active_streams = self._get_active_streams()
        print(f"[{AGENT_NAME}] Active streams: {active_streams}")

        rejected_text = (
            "\n".join(f"- {name}" for name in rejected_history)
            if rejected_history
            else "None yet."
        )
        return SCOUT_PROMPT.format(
            active_streams=active_streams,
            research_data=raw_data,
            rejected_history=rejected_text,
        )

    def _get_rejected_history(self) -> list[str]:
        """Return ignored + launched proposals across ALL platforms (no filter)."""
        try:
            rows = (
                supabase.table("scout_proposals")
                .select("opportunity_name")
                .in_("status", ["ignored", "launched"])
                .execute()
                .data
            )
            return [r["opportunity_name"] for r in rows if r.get("opportunity_name")]
        except Exception:
            return []

    def _on_proposals_saved(self, saved: list[dict]) -> None:
        """Enqueue a scout_proposal job so the orchestrator sees new proposals."""
        if not saved:
            return
        try:
            enqueue(
                "scout_proposal",
                payload={
                    "count":   len(saved),
                    "names":   [r.get("opportunity_name", "?") for r in saved],
                    "message": f"Scout found {len(saved)} new proposal(s). Review in dashboard.",
                },
            )
            print(f"[{AGENT_NAME}]   Enqueued scout_proposal job")
        except Exception as e:
            print(f"[{AGENT_NAME}]   Enqueue failed: {e}")

    @staticmethod
    def _get_active_streams() -> str:
        """Return comma-separated active income streams from Supabase."""
        try:
            streams: set[str] = set()
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
            if not streams:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                rows = (
                    supabase.table("research_briefs")
                    .select("platform")
                    .gte("created_at", cutoff)
                    .execute()
                    .data
                )
                for r in rows:
                    if r.get("platform"):
                        streams.add(r["platform"])
            return ", ".join(sorted(streams)) if streams else "etsy (setup phase)"
        except Exception:
            return "etsy (setup phase)"


# ─── Module-level public API (unchanged for scheduler/tests) ──────────────────

_scout = EtsyScout()


def run() -> list[dict]:
    """Full scout cycle: scrape all sources, evaluate, save, enqueue."""
    return _scout.run()


# ─── Test block ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="Force mock mode")
    args = parser.parse_args()
    if args.mock:
        MOCK_SCOUT = True
    run()
