"""
agents/publisher_agent.py — Publish approved Etsy designs via Printify.

Pipeline per design:
  1. Fetch approved, unpublished designs from Supabase (platform='etsy')
  2. Generate SEO-optimised listing copy (title, description, 13 tags) via Claude
  3. Upload design image + create Printify product
  4. Publish to Etsy via Printify's native sync (no Etsy API needed)
  5. Record listing in Supabase listings table
  6. Mark design as 'published' in Supabase designs table

Requires: PRINTIFY_API_KEY, PRINTIFY_SHOP_ID, ANTHROPIC_API_KEY
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from dotenv import load_dotenv

from core.supabase_client import supabase
from core.cost_logger import log_cost, calc_anthropic_cost
from core.spend_monitor import check_cap
from core.error_handler import api_call_with_retry
from publishers.etsy_printify import publish_design

load_dotenv()

AGENT_NAME = "publisher_agent"
MODEL      = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ROOT       = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DRAFT_MODE = os.getenv("DRAFT_MODE", "true").lower() == "true"


# ─── Listing copy generation ──────────────────────────────────────────────────

COPY_PROMPT = """\
You are an expert Etsy SEO copywriter for a print-on-demand gift mug store.

Design niche: {niche}
Design description / QA notes: {context}

Write an Etsy product listing for this design. Return ONLY valid JSON:

{{
  "title": "string — max 140 chars, front-load with the top keyword, natural language",
  "description": "string — 150-300 words, conversational, mentions the niche, gifting occasion, \
and quality. No bullet points. End with a call to action.",
  "tags": ["tag1", "tag2", ..., "tag13"]
}}

Tag rules:
- Exactly 13 tags
- Each tag max 20 characters
- Use long-tail keyword phrases buyers search for
- Include the niche, occupation, gift occasion (birthday, Christmas, etc.), and product type
- No single-word tags
- No tag can repeat words from the title
"""


def _generate_listing_copy(niche: str, context: str = "") -> dict | None:
    """
    Use Claude to generate SEO-optimised title, description, and 13 tags.
    Returns parsed dict or None on failure.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = COPY_PROMPT.format(niche=niche, context=context or niche)

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    in_tok  = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    cost    = calc_anthropic_cost(MODEL, in_tok, out_tok)
    log_cost(AGENT_NAME, "anthropic", MODEL, tokens_used=in_tok + out_tok, cost_usd=cost)

    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        data = json.loads(raw)
        if not all(k in data for k in ("title", "description", "tags")):
            raise ValueError("Missing required keys")
        if len(data["tags"]) != 13:
            # Pad or trim to exactly 13
            data["tags"] = (data["tags"] + [niche + " gift mug"] * 13)[:13]
        print(f"[{AGENT_NAME}]   Copy generated for '{niche}' — cost ${cost:.6f}")
        return data
    except Exception as e:
        print(f"[{AGENT_NAME}]   Copy parse failed: {e}")
        print(f"[{AGENT_NAME}]   Raw response: {raw[:300]}")
        return None


# ─── Supabase helpers ─────────────────────────────────────────────────────────

def _get_approved_designs() -> list[dict]:
    try:
        result = supabase.table("designs") \
            .select("id, niche, file_path, qa_reason, cost") \
            .eq("status", "approved") \
            .eq("platform", "etsy") \
            .order("created_at") \
            .execute()
        return result.data or []
    except Exception as e:
        print(f"[{AGENT_NAME}]   Could not fetch designs: {e}")
        return []


def _mark_published(design_id: str) -> None:
    try:
        supabase.table("designs").update({"status": "published"}).eq("id", design_id).execute()
    except Exception as e:
        print(f"[{AGENT_NAME}]   Could not mark design {design_id} as published: {e}")


def _record_listing(design_id: str, product_id: str, title: str, niche: str) -> None:
    try:
        supabase.table("listings").insert({
            "design_id":          design_id,
            "platform":           "etsy",
            "external_listing_id": product_id,
            "title":              title,
            "niche":              niche,
            "status":             "draft" if DRAFT_MODE else "active",
            "published_at":       datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"[{AGENT_NAME}]   Could not record listing: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(platform: str = "etsy") -> dict:
    """
    Publish all approved Etsy designs via Printify.
    Returns {"published": int, "failed": int, "cost": float}.
    """
    if platform != "etsy":
        print(f"[{AGENT_NAME}]   Platform '{platform}' not handled by this agent")
        return {"published": 0, "failed": 0, "cost": 0.0}

    print(f"[{AGENT_NAME}] --- Starting {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---")

    if not check_cap():
        print(f"[{AGENT_NAME}] Spend cap reached — exiting.")
        return {"published": 0, "failed": 0, "cost": 0.0}

    # Verify credentials before doing any work
    if not os.getenv("PRINTIFY_API_KEY", "").strip():
        print(f"[{AGENT_NAME}] PRINTIFY_API_KEY not set — cannot publish")
        return {"published": 0, "failed": 0, "cost": 0.0}
    if not os.getenv("PRINTIFY_SHOP_ID", "").strip():
        print(f"[{AGENT_NAME}] PRINTIFY_SHOP_ID not set — cannot publish")
        return {"published": 0, "failed": 0, "cost": 0.0}

    designs = _get_approved_designs()
    if not designs:
        print(f"[{AGENT_NAME}] No approved designs to publish.")
        return {"published": 0, "failed": 0, "cost": 0.0}

    print(f"[{AGENT_NAME}] {len(designs)} approved design(s) to publish")
    if DRAFT_MODE:
        print(f"[{AGENT_NAME}] DRAFT_MODE=true — listings created as drafts in Printify")

    published = failed = 0
    total_cost = 0.0

    for design in designs:
        design_id = design["id"]
        niche     = design.get("niche", "gift mug")
        file_path = design.get("file_path", "")

        print(f"\n[{AGENT_NAME}] Publishing design {design_id} — niche: '{niche}'")

        # Resolve file path (stored as relative in DB)
        abs_path = ROOT / file_path if file_path and not Path(file_path).is_absolute() else Path(file_path)
        if not abs_path.exists():
            print(f"[{AGENT_NAME}]   Image file not found: {abs_path} — skipping")
            failed += 1
            continue

        # Generate listing copy
        context = design.get("qa_reason") or ""
        copy = api_call_with_retry(
            lambda n=niche, c=context: _generate_listing_copy(n, c),
            max_retries=3,
            agent_name=AGENT_NAME,
        )
        if not copy:
            print(f"[{AGENT_NAME}]   Could not generate listing copy — skipping")
            failed += 1
            continue

        # Publish via Printify → Etsy
        product_id = api_call_with_retry(
            lambda t=copy["title"], d=copy["description"], tg=copy["tags"], p=str(abs_path):
                publish_design(t, d, tg, p),
            max_retries=2,
            agent_name=AGENT_NAME,
        )

        if product_id:
            _mark_published(design_id)
            _record_listing(design_id, product_id, copy["title"], niche)
            print(f"[{AGENT_NAME}]   Published — printify_product_id: {product_id}")
            published += 1
        else:
            print(f"[{AGENT_NAME}]   Publish failed for design {design_id}")
            failed += 1

    print(
        f"\n[{AGENT_NAME}] --- Done: {published} published, {failed} failed, "
        f"cost ${total_cost:.4f} ---"
    )
    return {"published": published, "failed": failed, "cost": total_cost}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--platform", default="etsy")
    args = parser.parse_args()
    if args.dry_run:
        designs = _get_approved_designs()
        print(f"[{AGENT_NAME}] Dry-run: {len(designs)} approved design(s) would be published")
    else:
        run(platform=args.platform)
