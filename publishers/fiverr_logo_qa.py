"""
publishers/fiverr_logo_qa.py — QA for Fiverr logo design delivery.

Inherits the shared GPT-4o vision pipeline from BaseQA (core/pipeline_base.py).
LogoQA adds logo-specific criteria: centering, white background, flat design,
simplicity, relevance to business type, and text readability.

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

MODULE_NAME = "fiverr_logo_qa"


# ─── Persona ──────────────────────────────────────────────────────────────────

_QA_SYSTEM = """\
You are a strict quality reviewer for a professional Fiverr logo design service.
Your job is to protect the seller's 5-star rating by ensuring every logo is
genuinely professional and ready for real business use before delivery.
A bad logo harms the buyer's brand — fail anything that would embarrass a professional designer.\
"""

# ─── Evaluation prompt template ───────────────────────────────────────────────

_QA_PROMPT_TEMPLATE = """\
Evaluate this logo design against the order requirements below.
Fail on ANY single issue — it is better to regenerate than deliver a substandard logo.

ORDER REQUIREMENTS:
- Business name: {business_name}
- Business type: {business_type}
- Industry: {industry}
- Color preferences: {colors}
- Style preferences: {style_preferences}

Before evaluating anything else, scan all four edges of the image.
If any logo element, text, or graphic touches or crosses any edge, FAIL IMMEDIATELY.

CHECK ALL OF THESE — fail on any single one:

1. CENTERING: Is the logo perfectly centered on the canvas with equal white space on all four sides?
   FAIL if the logo is noticeably off-center or positioned to one side.

2. MARGINS: Is there clear white space between the logo and all four edges?
   FAIL if any element is within 80px of any edge.

3. BACKGROUND: Is the background pure white with no gradients, textures, colors, or shadows?
   FAIL if the background is anything other than clean pure white.

4. TEXT READABILITY: Is the business name "{business_name}" clearly readable?
   FAIL if the text is too small, stylized to the point of illegibility, blurry, or missing.

5. SIMPLICITY: Is the design minimalist — clean flat design with no shadows, gradients, 3D effects,
   excessive detail, or visual noise?
   FAIL if the design has drop shadows, emboss effects, gradients, or looks overly complex.

6. RELEVANCE: Does the logo style match a {business_type} in the {industry} industry?
   FAIL if the icon or style is clearly inappropriate for this type of business.

7. COMPLETENESS: Are all logo elements (icon and text) fully visible with nothing cut off?
   FAIL immediately if any part of the logo touches or crosses any edge.

8. PROFESSIONALISM: Would a real {business_type} business actually use this logo?
   FAIL if it looks amateur, rushed, clip-art-like, or not suitable for business cards and websites.

Respond ONLY with valid JSON (no markdown):
{{
  "pass": true or false,
  "reason": "one sentence describing the main issue or why it passed",
  "suggested_fix": "one specific actionable sentence to fix the issue for the next attempt, or null if passed"
}}\
"""


# ─── Concrete QA class ────────────────────────────────────────────────────────

class LogoQA(BaseQA):
    """
    Logo-specific QA using GPT-4o vision.

    Checks centering, white background, flat design, simplicity, relevance,
    completeness, text readability, and overall professionalism.
    """

    platform      = MODULE_NAME
    system_prompt = _QA_SYSTEM

    def build_prompt(self, context: dict) -> str:
        biz_name   = (context.get("business_name") or "").strip() or "(not specified)"
        biz_type   = (context.get("business_type") or "business").strip()
        industry   = (context.get("industry") or "general").strip()
        colors     = (context.get("colors") or context.get("color_preferences") or "not specified").strip()
        style_pref = (context.get("style_preferences") or context.get("style_preference") or "not specified").strip()

        return _QA_PROMPT_TEMPLATE.format(
            business_name=biz_name,
            business_type=biz_type,
            industry=industry,
            colors=colors,
            style_preferences=style_pref,
        )


# ─── Module-level public API ──────────────────────────────────────────────────

_qa = LogoQA()


def evaluate(image_path: str, order: dict) -> dict:
    """
    Evaluate a generated logo against the specific order requirements.

    Args:
        image_path: Local path to the PNG file.
        order:      Parsed order dict with business_name, business_type, industry, etc.

    Returns:
        {"pass": bool, "reason": str, "suggested_fix": str|None,
         "cost": float, "tokens": int}
    """
    return _qa.qa_check(image_path, order)


# ─── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Path to logo PNG to evaluate")
    args = parser.parse_args()

    test_order = {
        "business_name":   "NovaBuild",
        "business_type":   "construction and renovation company",
        "industry":        "real_estate",
        "colors":          "navy blue and gold",
        "style_preferences": "modern, professional, trustworthy",
    }

    print(f"[{MODULE_NAME}] Evaluating: {args.image}")
    result = evaluate(args.image, test_order)
    print(json.dumps(result, indent=2))
