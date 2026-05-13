"""
publishers/fiverr_parser.py — Order parsing from Fiverr notification emails.

All functions are pure (no API calls, no DB writes) so they can be unit-tested
cheaply and called from fiverr_fulfillment.py without side effects.
"""

import os
import re
import sys
import uuid
from email import message as email_module
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).parent.parent

# ─── Niche detection ──────────────────────────────────────────────────────────

_NICHE_KEYWORDS: dict[str, list[str]] = {
    "gaming":    ["gaming", "game", "minecraft", "fortnite", "roblox", "gamer",
                  "twitch", "esports", "fps", "rpg", "streamer", "stream"],
    "finance":   ["finance", "money", "invest", "stock", "crypto", "income",
                  "wealth", "passive income", "trading", "budget", "earning",
                  "dropshipping", "business", "entrepreneur", "startup"],
    "fitness":   ["fitness", "workout", "gym", "exercise", "weight loss",
                  "muscle", "diet", "health", "nutrition", "bodybuilding",
                  "calories", "cardio", "keto"],
    "food":      ["food", "recipe", "cooking", "kitchen", "meal", "restaurant",
                  "baking", "chef", "eat", "cuisine", "taste", "delicious"],
    "tech":      ["tech", "technology", "software", "coding", "programming",
                  "ai", "chatgpt", "startup", "app", "python", "javascript",
                  "review", "phone", "laptop", "unboxing", "gadget"],
    "lifestyle": ["lifestyle", "vlog", "travel", "fashion", "beauty", "home",
                  "decor", "daily", "routine", "minimalist", "productivity"],
    "education": ["tutorial", "how to", "learn", "course", "education",
                  "explain", "guide", "tips", "tricks", "lesson", "study",
                  "beginner", "master"],
}

_REVISION_KEYWORDS = [
    "revision", "revise", "change", "update", "modify", "redo", "rework",
    "not happy", "different", "adjust", "fix", "wrong", "resubmit",
    "delivery revision", "order revision",
]


# ─── Public API ───────────────────────────────────────────────────────────────

def parse_order(email_body: str, attachments: list[str] = None) -> dict:
    """
    Extract all structured order fields from a Fiverr notification email body.

    Returns dict with keys:
      video_title, channel_niche, style_preference, has_face, buyer_images,
      color_preferences, text_to_include, revision_of, package_tier,
      special_instructions, requirements (raw text)
    """
    body    = email_body or ""
    attaches = attachments or []

    # ── Exact video title ──────────────────────────────────────────────────
    video_title = _extract_field(body, [
        r"video\s*title\s*[:\-]\s*(.{5,200})",
        r"title\s*[:\-]\s*(.{5,200})",
        r"video\s*[:\-]\s*(.{5,200})",
        r"thumbnail\s+for\s*[:\-]?\s*[\"']?(.{5,200})[\"']?",
    ])
    if not video_title:
        # Fall back: first sentence that looks like a video title (title-cased or ALL-CAPS)
        for line in body.splitlines():
            line = line.strip()
            if 10 < len(line) < 120 and (line.istitle() or line.isupper()):
                video_title = line
                break
    video_title = (video_title or "").strip()[:200]

    # ── Channel niche ──────────────────────────────────────────────────────
    explicit_niche = _extract_field(body, [
        r"(?:channel\s*)?niche\s*[:\-]\s*(\w[\w\s]{2,30})",
        r"channel\s*type\s*[:\-]\s*(\w[\w\s]{2,30})",
        r"channel\s*is\s*(?:about|for)\s*(\w[\w\s]{2,30})",
    ])
    channel_niche = _detect_niche(explicit_niche or body)

    # ── Style preference ───────────────────────────────────────────────────
    style_preference = _extract_field(body, [
        r"style\s*[:\-]\s*(.{3,150})",
        r"(?:make it|should be|i want)\s*(.{3,100})\s*(?:style|look|feel)",
        r"inspired\s*by\s*(.{3,100})",
    ]) or ""
    style_preference = style_preference.strip()[:200]

    # ── Face / person ──────────────────────────────────────────────────────
    has_face = bool(re.search(
        r"\b(face|person|myself|my photo|headshot|i want me|include me|put me)\b",
        body, re.IGNORECASE,
    ))

    # ── Colors ────────────────────────────────────────────────────────────
    color_prefs = _extract_field(body, [
        r"colou?rs?\s*[:\-]\s*(.{3,100})",
        r"(?:use|prefer|want)\s*((?:red|blue|green|yellow|purple|orange|pink|black|white|navy|gold|cyan|dark|bright|neon)[\w\s,/&+]+)",
    ]) or ""
    color_preferences = color_prefs.strip()[:150]

    # ── Exact text to include ─────────────────────────────────────────────
    text_to_include = _extract_field(body, [
        r"text\s*[:\-]\s*[\"']?(.{5,200})[\"']?",
        r"(?:write|include|display|show)\s*[:\-]?\s*[\"'](.{5,200})[\"']",
        r"thumbnail\s*text\s*[:\-]\s*(.{5,200})",
    ]) or video_title  # default to video title if not explicit

    # ── Revision of ───────────────────────────────────────────────────────
    revision_of = None
    if is_revision(body):
        rev_match = re.search(r"#?(FO\d{7,}|\d{7,})", body)
        revision_of = rev_match.group(1) if rev_match else "previous"

    # ── Package tier ──────────────────────────────────────────────────────
    package_tier = detect_package_tier(body)

    # ── Special instructions ───────────────────────────────────────────────
    special = _extract_field(body, [
        r"special\s*instructions?\s*[:\-]\s*(.{5,500})",
        r"additional\s*notes?\s*[:\-]\s*(.{5,500})",
        r"please\s+(.{5,300})",
    ]) or ""
    special_instructions = special.strip()[:400]

    return {
        "video_title":         video_title,
        "channel_niche":       channel_niche,
        "style_preference":    style_preference,
        "has_face":            has_face,
        "buyer_images":        attaches,
        "color_preferences":   color_preferences,
        "text_to_include":     text_to_include or video_title,
        "revision_of":         revision_of,
        "package_tier":        package_tier,
        "special_instructions": special_instructions,
        "requirements":        body[:1000].strip(),
    }


def is_revision(email_body: str) -> bool:
    """
    Return True if the email is a revision request rather than a new order.
    Checks for revision-related keywords and Fiverr's revision notification patterns.
    """
    body_lower = (email_body or "").lower()
    # Fiverr sends specific revision notification patterns
    if re.search(r"revision\s*request|requested\s*a\s*revision|order\s*revision", body_lower):
        return True
    # Keyword count: 2+ revision signals = likely a revision
    hits = sum(1 for kw in _REVISION_KEYWORDS if kw in body_lower)
    return hits >= 2


def extract_attachments(email_message) -> list[str]:
    """
    Walk the email MIME tree, save image attachments (PNG/JPEG) to a temp folder,
    and return their local file paths.

    Accepts either an email.message.Message object or raw bytes.
    """
    import email as email_lib

    if isinstance(email_message, bytes):
        email_message = email_lib.message_from_bytes(email_message)

    temp_dir = ROOT / "designs" / "fiverr" / "attachments"
    temp_dir.mkdir(parents=True, exist_ok=True)

    paths: list[str] = []
    image_types = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}

    if email_message.is_multipart():
        for part in email_message.walk():
            ct = part.get_content_type()
            if ct not in image_types:
                continue
            data = part.get_payload(decode=True)
            if not data:
                continue
            ext      = ct.split("/")[-1].replace("jpeg", "jpg")
            filename = f"attach_{uuid.uuid4().hex[:8]}.{ext}"
            fpath    = temp_dir / filename
            fpath.write_bytes(data)
            paths.append(str(fpath))

    return paths


def detect_package_tier(email_body: str) -> str:
    """Return 'basic', 'standard', or 'premium' from the email body."""
    body_lower = (email_body or "").lower()
    if "premium" in body_lower:
        return "premium"
    if "standard" in body_lower:
        return "standard"
    return "basic"


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _extract_field(body: str, patterns: list[str]) -> str | None:
    """Try each regex pattern in order, return the first captured group or None."""
    for pattern in patterns:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip(".,;")
    return None


def _detect_niche(text: str) -> str:
    """
    Score the text against each niche's keyword list.
    Returns the niche with the highest score, defaulting to 'lifestyle'.
    """
    text_lower = text.lower()
    scores: dict[str, int] = {niche: 0 for niche in _NICHE_KEYWORDS}
    for niche, keywords in _NICHE_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[niche] += 1
    best_niche  = max(scores, key=lambda n: scores[n])
    best_score  = scores[best_niche]
    return best_niche if best_score > 0 else "lifestyle"


# ─── Test ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample_email = """
New Order! Order #FO987654321

Hi, I just placed an order for 2 YouTube thumbnails.

Video title: I Tried Living on $5 a Day for 30 Days
Niche: Finance / personal finance
Style: Bold, MrBeast-style, high energy
Colors: bright green and black
Text: I TRIED LIVING ON $5 A DAY
I want a person (my face) in the thumbnail — I'll attach a photo separately.
Package: Standard

Special instructions: Please make it look very clickable, like a viral video.
"""
    order = parse_order(sample_email, attachments=[])
    import json
    print(json.dumps(order, indent=2))
    print(f"\nis_revision: {is_revision(sample_email)}")
    print(f"package_tier: {detect_package_tier(sample_email)}")
