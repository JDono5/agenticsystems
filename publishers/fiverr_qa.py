"""
publishers/fiverr_qa.py — Dedicated QA for Fiverr thumbnail delivery.

Inherits the shared GPT-4o vision pipeline from BaseQA (core/pipeline_base.py).
FiverrQA adds the thumbnail-specific criteria: order text visibility, niche
aesthetic match, no real-person resemblance, correct energy level, etc.

The module-level ``qa_thumbnail`` function is the public API — callers never
need to know about the class.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from core.pipeline_base import BaseQA

load_dotenv()

MODULE_NAME = "fiverr_qa"


# ─── Persona ──────────────────────────────────────────────────────────────────

_QA_SYSTEM = """\
You are a strict quality reviewer for a professional Fiverr YouTube thumbnail service.
Your job is to protect the seller's 5-star rating by catching every flaw before delivery.
The buyer paid for a specific outcome - fail anything that doesn't deliver it.\
"""

# ─── Evaluation prompt template ───────────────────────────────────────────────

_QA_PROMPT_TEMPLATE = """\
Evaluate this YouTube thumbnail against the specific order requirements below.
Fail on ANY single issue - it is better to regenerate than deliver a flawed product.

ORDER REQUIREMENTS:
- Required text: "{text_to_include}"
- Channel niche: {channel_niche}
- Person/face requested: {has_face}
- Style preference: {style_preference}
- Color preferences: {color_preferences}

CHECK ALL OF THESE - fail on any single one:

1. TEXT VISIBILITY: Is "{text_to_include}" fully visible, uncut, and perfectly spelled?
   FAIL if any letter is cut off at an edge, blurry, or partially hidden.

2. CROPPING: Are all elements (text, person, graphics) fully inside the canvas?
   FAIL if anything important is cropped at any edge.

3. NICHE AESTHETIC: Does it look like a real YouTube thumbnail for a {channel_niche} channel?
   FAIL if the style is completely wrong for the niche.

4. PERSON PRESENCE: {face_check_instruction}

5. PROFESSIONAL QUALITY: Would this thumbnail make someone click on a YouTube video?
   FAIL if it looks broken, unfinished, or would embarrass a professional service.

6. REAL PERSON RESEMBLANCE: Only FAIL if you can specifically name a real celebrity,
   YouTuber, or public figure this person looks like. If you cannot name a specific person,
   PASS this check. A generic person who looks professional or attractive is NOT a public
   figure resemblance. You must be able to say "this looks like [specific named person]"
   to fail this check. If in doubt, PASS.

7. ENERGY LEVEL: Is the visual energy appropriate for {channel_niche}?
   (gaming=intense, education=calm, finance=professional-exciting, fitness=high-energy)
   FAIL only if clearly wrong (e.g. extremely dull for a gaming thumbnail).

Respond ONLY with valid JSON (no markdown):
{{
  "pass": true or false,
  "reason": "one sentence describing the main issue or why it passed",
  "suggested_fix": "one specific, actionable sentence to fix the issue for the next attempt, or null if passed"
}}\
"""


# ─── Concrete QA class ────────────────────────────────────────────────────────

class FiverrQA(BaseQA):
    """
    Fiverr-specific thumbnail QA.

    Extends ``BaseQA`` with:
    - A reviewer persona (system_prompt) that frames the 5-star context
    - Per-order prompt building: extracts text, niche, face, style, colors
      from the order dict and assembles the 7-criterion evaluation prompt

    Adding a new platform (e.g. KDPQA) is the same shape:
      - Override system_prompt with the platform's reviewer persona
      - Override build_prompt to extract relevant fields from context
    """

    platform      = MODULE_NAME
    system_prompt = _QA_SYSTEM

    def build_prompt(self, context: dict) -> str:
        """Build the 7-criterion Fiverr thumbnail evaluation prompt."""
        text_req   = (context.get("text_to_include") or context.get("video_title") or "").strip()
        niche      = (context.get("channel_niche") or "lifestyle").strip()
        has_face   = bool(context.get("has_face"))
        style_pref = (context.get("style_preference") or "not specified").strip()
        colors     = (context.get("color_preferences") or "not specified").strip()

        face_check = (
            "A person with an expressive face must be clearly visible. FAIL if no person present."
            if has_face
            else "No face was requested. This check is N/A - do not fail for absence of person."
        )

        return _QA_PROMPT_TEMPLATE.format(
            text_to_include=text_req or "(no specific text requested)",
            channel_niche=niche,
            has_face="Yes" if has_face else "No",
            style_preference=style_pref,
            color_preferences=colors,
            face_check_instruction=face_check,
        )


# ─── Module-level public API (unchanged signature for all callers) ─────────────

_qa = FiverrQA()


def qa_thumbnail(image_path: str, order: dict) -> dict:
    """
    Evaluate a generated thumbnail against the specific order requirements.

    Args:
        image_path: Local path to the PNG file.
        order:      Parsed order dict from fiverr_parser.parse_order().

    Returns:
        {"pass": bool, "reason": str, "suggested_fix": str|None,
         "cost": float, "tokens": int}
    """
    return _qa.qa_check(image_path, order)


# ─── Test ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--image",
        default=str(Path(__file__).parent.parent / "assets" / "fiverr_samples" / "thumb_30days.png"),
    )
    args = parser.parse_args()

    test_order = {
        "video_title":       "I Ate Nothing But Chipotle for 30 Days",
        "channel_niche":     "lifestyle",
        "has_face":          True,
        "style_preference":  "MrBeast-style",
        "color_preferences": "red and black",
        "text_to_include":   "I ATE NOTHING BUT CHIPOTLE FOR 30 DAYS",
    }

    print(f"[{MODULE_NAME}] QA-ing: {args.image}")
    result = qa_thumbnail(args.image, test_order)
    print(json.dumps(result, indent=2))
