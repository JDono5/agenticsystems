"""
publishers/fiverr_scout.py — Fiverr-specific opportunity scout.

Inherits the shared orchestration loop (scrape → evaluate → save) from
BaseScout (core/pipeline_base.py).

FiverrScout adds:
  - 8 adjacent gig categories to investigate
  - Playwright scraper with MOCK_FIVERR fallback
  - Evaluation prompt tuned for "can our existing pipeline handle this?"

Scheduled: Sunday 7:15 AM (America/Chicago) via scheduler/main.py.
"""

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from core.pipeline_base import BaseScout

load_dotenv()

MODULE_NAME = "fiverr_scout"

# ─── Adjacent gig categories ──────────────────────────────────────────────────

ADJACENT_CATEGORIES = [
    {"name": "Twitch Panels",            "search": "twitch panels design",          "url_slug": "twitch-panels"},
    {"name": "YouTube End Screens",      "search": "youtube end screen template",   "url_slug": "youtube-end-screen"},
    {"name": "Channel Art / Banners",    "search": "youtube channel banner design", "url_slug": "youtube-channel-art"},
    {"name": "Podcast Cover Art",        "search": "podcast cover art design",      "url_slug": "podcast-cover"},
    {"name": "Instagram Story Templates","search": "instagram story template design","url_slug": "instagram-stories"},
    {"name": "Facebook Ad Creatives",    "search": "facebook ad creative design",   "url_slug": "facebook-ads-design"},
    {"name": "Twitch Overlays",          "search": "twitch overlay design",         "url_slug": "twitch-overlays"},
    {"name": "YouTube Shorts Thumbnails","search": "youtube shorts thumbnail",      "url_slug": "shorts-thumbnails"},
]

MOCK_GIG_DATA: list[dict] = [
    {
        "category": "Twitch Panels", "search_volume": "high", "avg_price": "$15-35",
        "avg_reviews": 47, "top_seller_level": "Level 2", "delivery_time": "1-2 days",
        "sample_listings": [
            "I will design professional twitch panels and overlays",
            "I will create custom twitch panels for your channel",
            "I will design 10 twitch panels in 24 hours",
        ],
    },
    {
        "category": "YouTube End Screens", "search_volume": "medium", "avg_price": "$10-25",
        "avg_reviews": 23, "top_seller_level": "Level 1", "delivery_time": "1-2 days",
        "sample_listings": [
            "I will design a professional youtube end screen",
            "I will create youtube end screen and cards templates",
        ],
    },
    {
        "category": "Podcast Cover Art", "search_volume": "high", "avg_price": "$20-50",
        "avg_reviews": 89, "top_seller_level": "Level 2", "delivery_time": "1-2 days",
        "sample_listings": [
            "I will design a professional podcast cover art",
            "I will create an eye-catching podcast artwork",
        ],
    },
    {
        "category": "Instagram Story Templates", "search_volume": "very high",
        "avg_price": "$25-60", "avg_reviews": 134, "top_seller_level": "Top Rated",
        "delivery_time": "2-3 days",
        "sample_listings": [
            "I will design instagram story templates in Canva",
            "I will create 10 custom instagram story templates",
        ],
    },
    {
        "category": "Facebook Ad Creatives", "search_volume": "very high",
        "avg_price": "$30-75", "avg_reviews": 211, "top_seller_level": "Top Rated",
        "delivery_time": "1-2 days",
        "sample_listings": [
            "I will design facebook ad creatives that convert",
            "I will create professional facebook and instagram ad graphics",
        ],
    },
]

# ─── Evaluation prompt ────────────────────────────────────────────────────────

_EVAL_PROMPT = """\
You are evaluating Fiverr gig categories as expansion opportunities for an existing
YouTube thumbnail service. The existing pipeline can generate any 1536x1024 flat
graphic design using GPT-4 image generation in under 5 minutes.

Current active gig: YouTube Custom Thumbnails (established, 5-star reviews goal).

CANDIDATE OPPORTUNITIES:
{raw_data}

PREVIOUSLY REJECTED (do not re-propose):
{rejected_history}

EVALUATION CRITERIA (ALL must be met to qualify):
1. Fulfillable: Can the existing image-generation pipeline handle this without new tools?
2. Free to launch: No upfront costs, no new accounts needed (uses same Fiverr account)
3. Revenue potential: Realistic $200+/month at typical volume
4. Adjacent: Close enough to thumbnails that the same buyer base would hire us
5. Differentiated: Something the market wants and we can do well with AI

For each category that passes all 5 criteria, output ONE JSON object per line (no arrays):
{{"opportunity_name": "...", "platform": "fiverr_expansion", "how_it_works": "...",
  "monthly_potential": 250, "setup_time_hours": 1, "risk_level": "low|medium|high",
  "why_now": "...", "pipeline_notes": "specific prompt changes needed"}}

Output ONLY qualifying opportunities (0 to 3 max). If none qualify, output nothing.
Do not wrap in markdown. Each opportunity on its own line.\
"""


# ─── Concrete scout class ─────────────────────────────────────────────────────

class FiverrScout(BaseScout):
    """
    Fiverr adjacent-gig opportunity scout.

    Inherits from BaseScout:
      - run()                    — full orchestration loop
      - _get_rejected_history()  — reads ignored proposals for this platform
      - _evaluate_with_claude()  — calls Claude, parses JSON lines / arrays
      - _save_proposals()        — writes to scout_proposals table

    Adds Fiverr-specific:
      - scrape_opportunities()   — Playwright scrape with mock fallback
      - _build_evaluation_prompt — formats adjacent-gig evaluation prompt
    """

    platform      = "fiverr_expansion"
    max_proposals = 3
    module_name   = MODULE_NAME

    def scrape_opportunities(self) -> list[dict]:
        """
        Scrape Fiverr adjacent-gig categories.
        Falls back to mock data on bot detection or Playwright failure.
        """
        if os.getenv("MOCK_FIVERR", "false").lower() == "true":
            print(f"[{MODULE_NAME}]   MOCK_FIVERR=true - using mock gig data")
            return MOCK_GIG_DATA

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(f"[{MODULE_NAME}]   Playwright not available - using mock data")
            return MOCK_GIG_DATA

        results: list[dict] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page    = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                )
                for cat in ADJACENT_CATEGORIES[:4]:
                    try:
                        url = (
                            "https://www.fiverr.com/search/gigs"
                            f"?query={cat['search'].replace(' ', '+')}"
                            "&sort=best_selling"
                        )
                        page.goto(url, timeout=15000)
                        page.wait_for_timeout(2000)

                        if ("recaptcha" in page.content().lower()
                                or "are you a robot" in page.content().lower()):
                            print(f"[{MODULE_NAME}]   Bot detected for {cat['name']} - using mock")
                            return MOCK_GIG_DATA

                        gig_cards = page.query_selector_all("[class*='gig-card']")
                        listings  = [
                            (card.query_selector("h3, [class*='title']") or {})
                            and card.query_selector("h3, [class*='title']").inner_text().strip()
                            for card in gig_cards[:5]
                            if card.query_selector("h3, [class*='title']")
                        ]
                        price_els    = page.query_selector_all("[class*='price']")
                        prices_found = [
                            el.inner_text().strip()
                            for el in price_els[:5]
                            if el.inner_text().strip().startswith("$")
                        ]
                        results.append({
                            "category":        cat["name"],
                            "search_volume":   "high" if len(gig_cards) >= 20 else "medium",
                            "avg_price":       prices_found[0] if prices_found else "$20-40",
                            "sample_listings": listings or [f"{cat['name']} design service"],
                        })
                        print(f"[{MODULE_NAME}]   Scraped {cat['name']}: {len(listings)} listings")
                    except Exception as e:
                        print(f"[{MODULE_NAME}]   Scrape failed for {cat['name']}: {e}")
                browser.close()
        except Exception as e:
            print(f"[{MODULE_NAME}]   Playwright error - using mock: {e}")
            return MOCK_GIG_DATA

        return results if results else MOCK_GIG_DATA

    def _build_evaluation_prompt(
        self,
        raw_data:         Any,
        rejected_history: list[str],
    ) -> str:
        gig_str      = json.dumps(raw_data, indent=2) if not isinstance(raw_data, str) else raw_data
        rejected_str = "\n".join(f"- {r}" for r in rejected_history) if rejected_history else "None"
        return _EVAL_PROMPT.format(raw_data=gig_str, rejected_history=rejected_str)


# ─── Module-level public API (unchanged signatures) ───────────────────────────

_scout = FiverrScout()


def scout_fiverr_opportunities() -> list[dict]:
    """
    Scrape Fiverr adjacent gig categories, evaluate via Claude, save proposals.
    Returns list of saved proposal dicts.
    """
    return _scout.run()


run = scout_fiverr_opportunities


# ─── Test block ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true",
                        help="Force mock Fiverr data (no real scraping)")
    args = parser.parse_args()

    if args.mock:
        os.environ["MOCK_FIVERR"] = "true"

    results = scout_fiverr_opportunities()
    print(f"\n[{MODULE_NAME}] Results:")
    for r in results:
        print(f"  - {r.get('opportunity_name')}: ${r.get('monthly_potential')}/month")
