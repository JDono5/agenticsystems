import os
import sys

# Allow running as `python agents/research_agent.py` from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import argparse
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

from core.supabase_client import save_brief
from core.cost_logger import log_cost, calc_anthropic_cost
from core.spend_monitor import check_cap
from core.error_handler import api_call_with_retry

load_dotenv()

AGENT_NAME = "research_agent"
MODEL      = "claude-sonnet-4-5"
ROOT       = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── Exact prompt from spec Section 6.1 ──────────────────────────────────────

RESEARCH_PROMPT = """\
You are a product research analyst for a print-on-demand store.

Platform: {platform}
Search query: {query}
Top {count} listings scraped today:
{listings_formatted}

Analyze this data and identify the single best design opportunity.

Return ONLY valid JSON, no markdown, no explanation:
{{
  "sub_niche": "specific sub-niche e.g. ICU nurse gifts",
  "opportunity_summary": "2-3 sentences on why this is the best opportunity right now",
  "top_keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"],
  "recommended_design_direction": "specific design concept",
  "recommended_price": 22.99,
  "avg_competitor_price": 0.00,
  "competitor_title_examples": ["title1", "title2", "title3"]
}}"""


# ─── Mock data (MOCK_ETSY=true / MOCK_FIVERR=true in .env) ───────────────────

MOCK_ETSY_LISTINGS: dict[str, list[dict]] = {
    "nurse gift mug": [
        {"title": "Funny Nurse Mug - I'm A Nurse What's Your Superpower",     "price": "$18.99", "review_count": "1243", "tags": ["nurse gift", "funny nurse mug", "rn gift"]},
        {"title": "Nurse Coffee Mug - Fueled By Coffee And Saving Lives",      "price": "$16.99", "review_count": "987",  "tags": ["nurse mug", "nurse life", "rn mug"]},
        {"title": "Personalized Nurse Mug - Best Nurse Ever Gift Idea",        "price": "$22.00", "review_count": "754",  "tags": ["personalized nurse", "nurse appreciation"]},
        {"title": "Nurse Mug - Yes I Am A Nurse No I Won't Look At It",        "price": "$15.99", "review_count": "612",  "tags": ["nurse humor", "funny nurse", "rn gift"]},
        {"title": "ICU Nurse Mug - I Can't Fix Stupid But I Can Sedate It",    "price": "$17.99", "review_count": "589",  "tags": ["icu nurse", "nurse humor", "medical gift"]},
    ],
    "teacher gift mug": [
        {"title": "Funny Teacher Mug - Teaching Is A Work Of Heart",           "price": "$17.99", "review_count": "2105", "tags": ["teacher gift", "teacher mug", "teacher life"]},
        {"title": "Teacher Coffee Mug - Powered By Coffee And Dry Erase",      "price": "$15.99", "review_count": "1432", "tags": ["teacher mug", "funny teacher", "educator gift"]},
        {"title": "Best Teacher Ever Mug - Appreciation Gift End Of Year",     "price": "$21.00", "review_count": "1087", "tags": ["best teacher", "teacher appreciation"]},
        {"title": "Teacher Mug - I Teach Tiny Humans What's Your Superpower",  "price": "$16.99", "review_count": "876",  "tags": ["elementary teacher", "funny teacher mug"]},
        {"title": "Math Teacher Mug - I Solve Problems For A Living",          "price": "$18.99", "review_count": "654",  "tags": ["math teacher", "math mug", "stem teacher"]},
    ],
    "electrician gift mug": [
        {"title": "Funny Electrician Mug - I Play With Wires And Won't Die",   "price": "$17.99", "review_count": "743",  "tags": ["electrician gift", "funny electrician"]},
        {"title": "Electrician Coffee Mug - Ohm My God I Love Coffee",         "price": "$16.99", "review_count": "621",  "tags": ["electrician mug", "ohm joke", "trades gift"]},
        {"title": "Master Electrician Mug - I'm Not Arguing I'm Just Right",   "price": "$18.99", "review_count": "489",  "tags": ["master electrician", "tradesmen gift"]},
        {"title": "Electrician Mug - Watt Do You Want From Me",                "price": "$15.99", "review_count": "412",  "tags": ["electrician pun", "watt joke", "trades humor"]},
        {"title": "Electrician Dad Mug - Powered By Coffee And High Voltage",  "price": "$19.99", "review_count": "387",  "tags": ["electrician dad", "father gift", "trades dad"]},
    ],
    "plumber gift mug": [
        {"title": "Funny Plumber Mug - I Fix Shit Literally",                  "price": "$17.99", "review_count": "612",  "tags": ["plumber gift", "plumber mug", "plumber humor"]},
        {"title": "Plumber Coffee Mug - Happiness Is A Good Flush",            "price": "$16.99", "review_count": "498",  "tags": ["plumber mug", "plumber humor", "trades gift"]},
        {"title": "Master Plumber Mug - No Job Too Big No Leak Too Small",     "price": "$18.99", "review_count": "387",  "tags": ["master plumber", "plumber gift", "trades mug"]},
    ],
    "firefighter gift mug": [
        {"title": "Firefighter Mug - I Run Into Burning Buildings For Fun",    "price": "$18.99", "review_count": "891",  "tags": ["firefighter gift", "fire dept", "first responder"]},
        {"title": "Fire Fighter Coffee Mug - Brave Enough To Be A Hero",       "price": "$17.99", "review_count": "743",  "tags": ["firefighter mug", "hero gift", "fireman gift"]},
        {"title": "Firefighter Mug - Saving Lives Drinking Coffee Repeat",     "price": "$16.99", "review_count": "612",  "tags": ["firefighter coffee", "fire station gift"]},
    ],
    "engineer gift mug": [
        {"title": "Engineer Mug - I Solve Problems You Didn't Know You Had",   "price": "$18.99", "review_count": "1123", "tags": ["engineer gift", "engineering mug", "stem gift"]},
        {"title": "Software Engineer Mug - It Works On My Machine",            "price": "$17.99", "review_count": "987",  "tags": ["software engineer", "programmer gift", "developer mug"]},
        {"title": "Civil Engineer Mug - I Move The Earth For Fun",             "price": "$16.99", "review_count": "654",  "tags": ["civil engineer", "engineer humor", "construction gift"]},
    ],
}

MOCK_FIVERR_LISTINGS: dict[str, list[dict]] = {
    "youtube thumbnail design": [
        {"title": "I will design viral YouTube thumbnails that boost your CTR",           "seller_level": "Level 2", "review_count": "2341", "starting_price": "$10", "delivery_days": "1"},
        {"title": "I will create professional custom YouTube thumbnails fast",            "seller_level": "Level 2", "review_count": "1876", "starting_price": "$10", "delivery_days": "1"},
        {"title": "I will design eye-catching YouTube thumbnails for any niche",          "seller_level": "Level 1", "review_count": "987",  "starting_price": "$8",  "delivery_days": "1"},
        {"title": "I will design clickbait YouTube thumbnails that actually work",        "seller_level": "Top Rated", "review_count": "3210", "starting_price": "$15", "delivery_days": "1"},
        {"title": "I will create stunning minimalist YouTube thumbnail designs",          "seller_level": "Level 2", "review_count": "1432", "starting_price": "$12", "delivery_days": "1"},
    ],
    "youtube thumbnail gaming": [
        {"title": "I will design epic gaming YouTube thumbnails with 3D effects",         "seller_level": "Level 2", "review_count": "1654", "starting_price": "$12", "delivery_days": "1"},
        {"title": "I will create Fortnite Minecraft gaming channel thumbnails",           "seller_level": "Level 1", "review_count": "876",  "starting_price": "$10", "delivery_days": "1"},
        {"title": "I will design pro gaming YouTube thumbnails like top channels",        "seller_level": "Level 2", "review_count": "1234", "starting_price": "$10", "delivery_days": "1"},
    ],
    "youtube thumbnail fitness": [
        {"title": "I will design motivational fitness YouTube thumbnails",                "seller_level": "Level 2", "review_count": "987",  "starting_price": "$10", "delivery_days": "1"},
        {"title": "I will create gym workout YouTube thumbnails for fitness creators",    "seller_level": "Level 1", "review_count": "654",  "starting_price": "$8",  "delivery_days": "1"},
    ],
    "youtube thumbnail finance": [
        {"title": "I will design professional finance and investing YouTube thumbnails",  "seller_level": "Level 2", "review_count": "1123", "starting_price": "$12", "delivery_days": "1"},
        {"title": "I will create clean money business YouTube channel thumbnails",        "seller_level": "Level 1", "review_count": "765",  "starting_price": "$10", "delivery_days": "1"},
    ],
}


def _fetch_mock_etsy(query: str) -> list[dict]:
    listings = MOCK_ETSY_LISTINGS.get(query, list(MOCK_ETSY_LISTINGS.values())[0])
    print(f"[{AGENT_NAME}]   Mock mode (Etsy): {len(listings)} sample listings")
    return listings


def _fetch_mock_fiverr(query: str) -> list[dict]:
    listings = MOCK_FIVERR_LISTINGS.get(query, list(MOCK_FIVERR_LISTINGS.values())[0])
    print(f"[{AGENT_NAME}]   Mock mode (Fiverr): {len(listings)} sample listings")
    return listings


# ─── Etsy public scraper (no API key required) ────────────────────────────────
#
# This scraper uses Playwright to fetch public Etsy search result pages —
# exactly the same pages any browser visitor would see. No Etsy API credentials,
# no developer app approval, and no OAuth tokens are required or used.
# It reads publicly visible listing titles, prices, favourite counts, and tags
# from etsy.com/search the same way a human browsing the site would.

def _scrape_etsy(query: str, limit: int = 20) -> list[dict]:
    """
    Scrape Etsy search results from public pages using Playwright.
    No API key needed — reads the same HTML any browser would see.
    Falls back gracefully and returns an empty list on any failure.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    url = f"https://www.etsy.com/search?q={requests.utils.quote(query)}&explicit=1"
    print(f"[{AGENT_NAME}]   Scraping Etsy (public): {url}")

    listings = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2500)

            # Each result card sits inside a <li> with a data-palette-listing-id attr
            cards = page.query_selector_all("li[data-palette-listing-id]")
            if not cards:
                # Fallback selector for A/B layout variants
                cards = page.query_selector_all("[data-listing-id]")

            for card in cards[:limit]:
                try:
                    title_el = (
                        card.query_selector("h3")
                        or card.query_selector("[class*='listing-title']")
                        or card.query_selector("a[title]")
                    )
                    title = title_el.inner_text().strip() if title_el else ""
                    if not title and title_el:
                        title = title_el.get_attribute("title") or ""

                    price_el = card.query_selector("[class*='currency-value'], [class*='price']")
                    price = f"${price_el.inner_text().strip()}" if price_el else "N/A"

                    fav_el = card.query_selector("[class*='favorite'], [class*='heart']")
                    favs = re.sub(r"[^\d]", "", fav_el.inner_text()) if fav_el else "0"

                    if title:
                        listings.append({
                            "title":        title,
                            "price":        price,
                            "review_count": favs or "0",
                            "tags":         [],
                        })
                except Exception:
                    continue

            browser.close()
    except PWTimeout:
        print(f"[{AGENT_NAME}]   Etsy scrape timed out — no results")
    except Exception as e:
        print(f"[{AGENT_NAME}]   Etsy scrape error: {e}")

    print(f"[{AGENT_NAME}]   Scraped {len(listings)} Etsy listings for '{query}'")
    return listings


# ─── Fiverr Playwright scraper ────────────────────────────────────────────────

def _scrape_fiverr(query: str, limit: int = 20) -> list[dict]:
    """
    Scrape Fiverr gig search results using Playwright.
    Falls back gracefully — returns empty list on any failure.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    url = (
        f"https://www.fiverr.com/search/gigs"
        f"?query={requests.utils.quote(query)}&sort_by=best_selling"
    )
    print(f"[{AGENT_NAME}]   Scraping Fiverr: {url}")

    gigs = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3000)

            cards = page.query_selector_all("[class*='gig-card'], [data-testid*='gig']")
            if not cards:
                cards = page.query_selector_all("li[class*='gig']")

            for card in cards[:limit]:
                try:
                    title_el = (
                        card.query_selector("h3")
                        or card.query_selector("[class*='title']")
                    )
                    title = title_el.inner_text().strip() if title_el else ""

                    price_el = card.query_selector("[class*='price']")
                    price = price_el.inner_text().strip() if price_el else "N/A"

                    reviews_el = card.query_selector("[class*='rating'] span") or card.query_selector("[class*='review']")
                    review_count = reviews_el.inner_text().strip() if reviews_el else "0"

                    badge_el = card.query_selector("[class*='seller-level'], [class*='badge']")
                    seller_level = badge_el.inner_text().strip() if badge_el else "New Seller"

                    if title:
                        gigs.append({
                            "title":         title,
                            "seller_level":  seller_level,
                            "review_count":  re.sub(r"[^\d]", "", review_count) or "0",
                            "starting_price": price,
                            "delivery_days": "1",
                        })
                except Exception:
                    continue

            browser.close()
    except PWTimeout:
        print(f"[{AGENT_NAME}]   Fiverr scrape timed out - no results")
    except Exception as e:
        print(f"[{AGENT_NAME}]   Fiverr scrape error: {e}")

    print(f"[{AGENT_NAME}]   Scraped {len(gigs)} Fiverr gigs for '{query}'")
    return gigs


# ─── Listing formatter ────────────────────────────────────────────────────────

def _format_listings(listings: list[dict], platform: str) -> str:
    lines = []
    for i, item in enumerate(listings, 1):
        if platform == "etsy":
            tags = ", ".join(item.get("tags", [])[:5]) or "N/A"
            lines.append(
                f"{i}. Title:        {item.get('title','')}\n"
                f"   Price:        {item.get('price','N/A')}\n"
                f"   Favorites:    {item.get('review_count','N/A')}\n"
                f"   Tags:         {tags}"
            )
        else:  # fiverr
            lines.append(
                f"{i}. Title:        {item.get('title','')}\n"
                f"   Seller Level: {item.get('seller_level','N/A')}\n"
                f"   Reviews:      {item.get('review_count','0')}\n"
                f"   Starting at:  {item.get('starting_price','N/A')}\n"
                f"   Delivery:     {item.get('delivery_days','N/A')} day(s)"
            )
    return "\n".join(lines)


# ─── Claude brief generation ──────────────────────────────────────────────────

def _generate_brief(platform: str, query: str, listings: list[dict]) -> dict:
    """
    Send listing data to Claude, parse the JSON response, return a brief dict.
    Cost is logged immediately after the API call regardless of parse success.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    listings_text = _format_listings(listings, platform)
    prompt = RESEARCH_PROMPT.format(
        platform=platform,
        query=query,
        count=len(listings),
        listings_formatted=listings_text,
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    input_tokens  = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost          = calc_anthropic_cost(MODEL, input_tokens, output_tokens)
    log_cost(
        agent=AGENT_NAME,
        provider="anthropic",
        model=MODEL,
        tokens_used=input_tokens + output_tokens,
        cost_usd=cost,
    )

    # Parse JSON — strip markdown fences; log error and re-raise on parse failure
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.rstrip())

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[{AGENT_NAME}]   JSON parse failed for '{query}': {e}")
        raise

    # Normalise price field
    prices = []
    for item in listings:
        raw_price = str(item.get("price") or item.get("starting_price") or "")
        m = re.search(r"[\d]+\.?\d*", raw_price.replace(",", "").replace("$", ""))
        if m:
            try:
                prices.append(float(m.group(0)))
            except ValueError:
                pass
    avg_price = round(sum(prices) / len(prices), 2) if prices else float(data.get("avg_competitor_price", 0))

    brief = {
        "platform":                    platform,
        "niche":                       f"funny occupation gifts" if platform == "etsy" else "youtube thumbnails",
        "sub_niche":                   data.get("sub_niche", query),
        "top_keywords":                data.get("top_keywords", []),
        "opportunity_summary":         data.get("opportunity_summary", ""),
        "top_competitor_titles":       data.get("competitor_title_examples", []),
        "avg_price_point":             avg_price,
        # Extra fields from v5 schema
        "recommended_design_direction": data.get("recommended_design_direction", ""),
    }

    print(f"[{AGENT_NAME}]   Brief generated - sub_niche: '{brief['sub_niche']}' - cost ${cost:.6f}")
    return brief


# ─── Main entry point ─────────────────────────────────────────────────────────

def run(platform: str = "etsy") -> list[dict]:
    """
    Run a full research cycle for the given platform.

    Loads search queries from platform_config/{platform}.json.
    Returns a list of saved brief dicts.
    """
    print(
        f"[{AGENT_NAME}] --- Starting ({platform}) "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---"
    )

    if not check_cap():
        print(f"[{AGENT_NAME}] Spend cap reached - exiting.")
        return []

    # Load platform config
    config_path = ROOT / "platform_config" / f"{platform}.json"
    if not config_path.exists():
        print(f"[{AGENT_NAME}] Config not found: {config_path}")
        return []
    config  = json.loads(config_path.read_text())
    queries = config["research"]["search_queries"]

    mock_etsy   = os.getenv("MOCK_ETSY", "false").lower() == "true"
    mock_fiverr = os.getenv("MOCK_FIVERR", "false").lower() == "true"

    if platform == "etsy" and mock_etsy:
        print(f"[{AGENT_NAME}] MOCK_ETSY=true - using sample data")
    if platform == "fiverr" and mock_fiverr:
        print(f"[{AGENT_NAME}] MOCK_FIVERR=true - using sample data")

    saved_briefs: list[dict] = []

    for query in queries:
        print(f"\n[{AGENT_NAME}] Query: '{query}'")

        # Step 1: fetch listings
        if platform == "etsy":
            if mock_etsy:
                listings = _fetch_mock_etsy(query)
            else:
                listings = api_call_with_retry(
                    lambda q=query: _scrape_etsy(q),
                    max_retries=2,
                    agent_name=AGENT_NAME,
                )
        else:  # fiverr
            if mock_fiverr:
                listings = _fetch_mock_fiverr(query)
            else:
                listings = api_call_with_retry(
                    lambda q=query: _scrape_fiverr(q),
                    max_retries=2,
                    agent_name=AGENT_NAME,
                )

        if not listings:
            print(f"[{AGENT_NAME}]   No listings returned - skipping.")
            continue

        # Step 2: generate brief via Claude
        brief = api_call_with_retry(
            lambda p=platform, q=query, l=listings: _generate_brief(p, q, l),
            max_retries=3,
            agent_name=AGENT_NAME,
        )
        if not brief:
            print(f"[{AGENT_NAME}]   Brief generation failed - skipping.")
            continue

        # Step 3: save to Supabase
        saved = api_call_with_retry(
            lambda b=brief: save_brief(b),
            max_retries=3,
            agent_name=AGENT_NAME,
        )
        if saved:
            print(f"[{AGENT_NAME}]   Saved - id: {saved['id']}")
            saved_briefs.append(saved)

            # Step 4: enqueue research_complete job (graceful — table may not exist yet)
            try:
                from core.job_queue import enqueue
                enqueue("research_complete", platform=platform, payload={"brief_id": saved["id"]})
            except Exception:
                pass

    print(f"\n[{AGENT_NAME}] --- Run complete: {len(saved_briefs)} brief(s) saved ---")
    return saved_briefs


# Backward-compat alias used by scheduler
run_research = run


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the research agent")
    parser.add_argument("--platform", choices=["etsy", "fiverr"], default="etsy",
                        help="Platform to research (default: etsy)")
    args = parser.parse_args()
    run(platform=args.platform)
