"""
agents/fiverr_order_parser.py — Claude-powered Fiverr order email parser.

Reads a raw Fiverr order notification email and uses Claude to intelligently
extract structured order data regardless of formatting variations.

Extracted fields:
  order_id         — Fiverr order number (e.g. FO1234567890)
  package_tier     — basic / standard / premium
  gig_type         — thumbnail / logo / social_media (detected from gig title in email)
  buyer_name       — buyer's Fiverr username
  buyer_answers    — dict of {question: answer} for all requirement questions
  attached_files   — list of attachment filenames or URLs found in the email
  raw_requirements — full requirements text block for fallback use

Run standalone:
  python agents/fiverr_order_parser.py --email path/to/email.txt
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODULE_NAME   = "fiverr_order_parser"
CLAUDE_MODEL  = "claude-sonnet-4-5"

# Keywords that identify each gig type from the email subject/gig title
_GIG_TYPE_KEYWORDS: dict[str, list[str]] = {
    "logo": [
        "logo", "brand identity", "brand design", "icon design",
        "wordmark", "logotype", "brand logo",
    ],
    "social_media": [
        "instagram", "social media", "social post", "ig post",
        "post graphic", "social graphic", "content graphic",
        "instagram post", "social content",
    ],
    "thumbnail": [
        "thumbnail", "youtube thumbnail", "yt thumbnail",
        "video thumbnail", "channel art",
    ],
}

_PARSE_SYSTEM = """\
You are an expert at extracting structured data from Fiverr order notification emails.
Fiverr emails can vary in format — your job is to reliably extract the key fields
regardless of layout. Return only valid JSON, no markdown, no explanation.\
"""

_PARSE_PROMPT = """\
Extract structured order information from the following Fiverr order email.

EMAIL:
---
{email_text}
---

Return ONLY valid JSON with these exact keys:
{{
  "order_id": "string or null — Fiverr order number, usually starts with FO or is a number",
  "package_tier": "basic or standard or premium — infer from package name/price if not explicit",
  "gig_type_hint": "string or null — the exact gig title or type mentioned in the email",
  "buyer_name": "string or null — the buyer's Fiverr username",
  "buyer_answers": {{
    "question text": "answer text"
  }},
  "attached_files": ["list of any filenames or image URLs mentioned as attachments"],
  "raw_requirements": "string — the full requirements / order details text block, verbatim"
}}

Rules:
- buyer_answers should capture ALL questions and answers from the requirements section
- If a field is not found, use null (not an empty string)
- For package_tier: 'basic' if cheap/starter/1 deliverable, 'premium' if top tier, else 'standard'
- attached_files: include any image uploads, file links, or attachment references
- raw_requirements: copy the entire requirements block as-is for fallback use
\
"""


def _detect_gig_type_from_email(email_text: str, gig_type_hint: str | None) -> str:
    """
    Determine gig type by scanning the email and Claude's extracted hint for keywords.
    Falls back to 'thumbnail' if nothing matches.
    """
    search_text = " ".join([
        (email_text or "").lower(),
        (gig_type_hint or "").lower(),
    ])

    for gig_type, keywords in _GIG_TYPE_KEYWORDS.items():
        if any(kw in search_text for kw in keywords):
            return gig_type

    return "thumbnail"


def parse_order_email(email_text: str) -> dict:
    """
    Parse a raw Fiverr order email using Claude.

    Args:
        email_text: Raw text of the Fiverr order notification email.

    Returns:
        Structured order dict with keys:
          order_id, package_tier, gig_type, buyer_name,
          buyer_answers, attached_files, raw_requirements
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = _PARSE_PROMPT.format(email_text=email_text[:8000])

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=_PARSE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        parsed = json.loads(raw)

    except json.JSONDecodeError as e:
        print(f"[{MODULE_NAME}] JSON decode error: {e} — using empty fallback")
        parsed = {}
    except Exception as e:
        print(f"[{MODULE_NAME}] Claude parse error: {e}")
        parsed = {}

    # Resolve gig_type using both Claude's hint and keyword scan
    gig_type = _detect_gig_type_from_email(
        email_text,
        parsed.get("gig_type_hint"),
    )

    order = {
        "order_id":        parsed.get("order_id"),
        "package_tier":    parsed.get("package_tier") or "standard",
        "gig_type":        gig_type,
        "buyer_name":      parsed.get("buyer_name"),
        "buyer_answers":   parsed.get("buyer_answers") or {},
        "attached_files":  parsed.get("attached_files") or [],
        "raw_requirements": parsed.get("raw_requirements") or email_text[:2000],
    }

    # Flatten buyer_answers into top-level order keys for downstream compatibility
    # Common thumbnail fields
    answers = order["buyer_answers"]
    order.setdefault("video_title",         _first_value(answers, ["video title", "youtube video title", "title"]))
    order.setdefault("channel_niche",       _first_value(answers, ["niche", "channel niche", "channel topic", "type of channel"]))
    order.setdefault("style_preference",    _first_value(answers, ["style", "style preference", "design style", "thumbnail style"]))
    order.setdefault("color_preferences",   _first_value(answers, ["colors", "color preferences", "colour", "color scheme"]))
    order.setdefault("text_to_include",     _first_value(answers, ["text", "text to include", "overlay text", "words on thumbnail"]))
    order.setdefault("special_instructions", _first_value(answers, ["special instructions", "additional notes", "other", "anything else"]))

    # Logo-specific fields
    order.setdefault("business_name",       _first_value(answers, ["business name", "company name", "brand name", "name"]))
    order.setdefault("business_type",       _first_value(answers, ["business type", "industry", "type of business", "what does your business do"]))
    order.setdefault("industry",            _first_value(answers, ["industry", "business category", "sector"]))
    order.setdefault("style_preferences",   order.get("style_preference"))

    # Social media fields
    order.setdefault("style",               order.get("style_preference"))
    order.setdefault("colors",              order.get("color_preferences"))

    print(f"[{MODULE_NAME}] Parsed: order_id={order['order_id']} "
          f"gig_type={order['gig_type']} tier={order['package_tier']}")

    return order


def _first_value(answers: dict, candidates: list[str]) -> str | None:
    """Return the first answer whose question key fuzzy-matches any candidate."""
    if not answers:
        return None
    for key, val in answers.items():
        for candidate in candidates:
            if candidate in key.lower():
                return val if isinstance(val, str) else str(val)
    return None


# ─── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse a Fiverr order email with Claude")
    parser.add_argument("--email", required=True, help="Path to a .txt file with the raw email body")
    args = parser.parse_args()

    email_path = Path(args.email)
    if not email_path.exists():
        print(f"[{MODULE_NAME}] File not found: {email_path}")
        sys.exit(1)

    email_text = email_path.read_text(encoding="utf-8", errors="replace")
    result = parse_order_email(email_text)
    print(json.dumps(result, indent=2))
