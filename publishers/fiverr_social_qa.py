"""
publishers/fiverr_social_qa.py — QA for Fiverr Instagram social media post delivery.

Inherits the shared GPT-4o vision pipeline from BaseQA (core/pipeline_base.py).
SocialQA adds post-specific criteria: text readability, visual hierarchy,
brand consistency, professionalism, and square composition completeness.

Public API: evaluate(image_path, order) -> dict
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from core.pipeline_base import BaseQA

load_dotenv()

MODULE_NAME = "fiverr_social_qa"


# ─── Persona ──────────────────────────────────────────────────────────────────

_QA_SYSTEM = """\
You are a strict quality reviewer for a professional Fiverr social media design service.
Your job is to protect the seller's 5-star rating by ensuring every Instagram post
graphic is genuinely professional and ready to post without any edits.
A blurry, cluttered, or off-brand post reflects poorly on the buyer's business — fail
anything that wouldn't look at home on a real brand's Instagram feed.\
"""

# ─── Evaluation prompt template ───────────────────────────────────────────────

_QA_PROMPT_TEMPLATE = """\
Evaluate this Instagram post graphic against the order requirements below.
Fail on ANY single issue — it is better to regenerate than deliver a substandard post.

ORDER REQUIREMENTS:
- Business name: {business_name}
- Business type: {business_type}
- Brand colors: {colors}
- Style: {style}
- Post theme: {post_theme}

Before evaluating anything else, scan all four edges of the image.
If any text or graphic element touches or crosses any edge, FAIL IMMEDIATELY.

CHECK ALL OF THESE — fail on any single one:

1. DIMENSIONS / COMPLETENESS: Is this a clean square composition?
   Is everything fully contained within the frame with clear margins from all edges?
   FAIL if any element is cut off or touches any edge.

2. TEXT READABILITY: Is all text fully readable and legible at small sizes (as seen in a phone feed)?
   FAIL if any text is cut off, too small to read, blurry, or poorly contrasted with the background.

3. VISUAL HIERARCHY: Is there a clear dominant headline that draws the eye first,
   with supporting text appropriately smaller?
   FAIL if all text elements are the same size or there is no clear focal point.

4. BRAND CONSISTENCY: Do the colors and overall style match "{business_type}" and
   the brief colors ({colors})?
   FAIL if the design uses completely different colors or a style mismatched to the business type.

5. PROFESSIONALISM: Does this look like a real post from a professional {business_type} brand?
   FAIL if it looks amateurish, cluttered, has too many competing elements, or uses clip-art.

6. CONTENT APPROPRIATENESS: Is the post theme ({post_theme}) clearly communicated visually?
   FAIL if a viewer cannot tell what the post is about within 2 seconds.

Respond ONLY with valid JSON (no markdown):
{{
  "pass": true or false,
  "reason": "one sentence describing the main issue or why it passed",
  "suggested_fix": "one specific actionable sentence to fix the issue for the next attempt, or null if passed"
}}\
"""


# ─── Concrete QA class ────────────────────────────────────────────────────────

class SocialQA(BaseQA):
    """
    Instagram post QA using GPT-4o vision.

    Checks square completeness, text readability, visual hierarchy,
    brand consistency, and overall professionalism.
    """

    platform      = MODULE_NAME
    system_prompt = _QA_SYSTEM

    def build_prompt(self, context: dict) -> str:
        biz_name   = (context.get("business_name") or "").strip() or "(not specified)"
        biz_type   = (context.get("business_type") or "business").strip()
        colors     = (context.get("colors") or context.get("color_preferences") or "not specified").strip()
        style      = (context.get("style") or context.get("style_preference") or "clean modern").strip()
        post_theme = (context.get("post_theme") or context.get("theme_label") or "general").strip()

        return _QA_PROMPT_TEMPLATE.format(
            business_name=biz_name,
            business_type=biz_type,
            colors=colors,
            style=style,
            post_theme=post_theme,
        )


# ─── Module-level public API ──────────────────────────────────────────────────

_qa = SocialQA()


def evaluate(image_path: str, order: dict) -> dict:
    """
    Evaluate a generated Instagram post graphic against the specific order requirements.

    Args:
        image_path: Local path to the PNG file.
        order:      Parsed order dict with business_name, business_type, colors, etc.
                    May also include post_theme / theme_label from the generation step.

    Returns:
        {"pass": bool, "reason": str, "suggested_fix": str|None,
         "cost": float, "tokens": int}
    """
    return _qa.qa_check(image_path, order)


# ─── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Path to social post PNG to evaluate")
    args = parser.parse_args()

    test_order = {
        "business_name":  "Bloom Bakery",
        "business_type":  "artisan bakery",
        "colors":         "pastel pink and cream",
        "style":          "elegant, warm",
        "post_theme":     "Inspirational Quote",
        "theme_label":    "Inspirational Quote",
    }

    print(f"[{MODULE_NAME}] Evaluating: {args.image}")
    result = evaluate(args.image, test_order)
    print(json.dumps(result, indent=2))
