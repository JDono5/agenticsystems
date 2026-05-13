"""
publishers/fiverr_analyzer.py — GPT-4o vision analysis of buyer-provided images.

Takes local image file paths (already downloaded by fiverr_parser.extract_attachments)
and returns a style_context dict for injection into the thumbnail prompt.

Cost: ~$0.003-0.005 per image (GPT-4o vision, detail=low).
"""

import base64
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai
from dotenv import load_dotenv

from core.cost_logger import log_cost, calc_openai_cost

load_dotenv()

MODULE_NAME  = "fiverr_analyzer"
VISION_MODEL = "gpt-4o"


# ─── Vision prompt ────────────────────────────────────────────────────────────

_ANALYSIS_PROMPT = """\
Analyze this image. First identify what type it is:
  A) A person or headshot photo
  B) An existing YouTube thumbnail
  C) A YouTube channel page / screenshot
  D) A reference image or mood board

Then provide a JSON object with these keys:
{
  "image_type": "person|thumbnail|channel|reference",
  "dominant_colors": "comma-separated list of 2-4 dominant colors",
  "energy_level": "calm | professional | moderate | energetic | intense",
  "layout_pattern": "e.g. person-right-text-left, centered-icon, face-only, split-background",
  "font_style": "e.g. bold-impact, clean-sans-serif, decorative, handwritten — or null if not visible",
  "aesthetic": "1-2 sentence description of the overall visual style",
  "person_description": "if a person is visible: their approximate age, style, expression — else null",
  "design_notes": "2-3 specific notes that would help recreate or match this visual style in a new thumbnail"
}

Return ONLY valid JSON. No markdown, no explanation.\
"""


# ─── Public API ───────────────────────────────────────────────────────────────

def is_face_photo(image_path: str) -> bool:
    """
    GPT-4o vision: determine whether image_path is a real person's photo
    (Case A — buyer wants their face composited) vs. a reference thumbnail
    for style extraction only (Case B).

    Returns True if it is a real person's photo, False if it is a reference thumbnail.
    """
    path = Path(image_path)
    if not path.exists():
        return False

    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    prompt = (
        "Look at this image carefully. Answer ONE question only.\n\n"
        "Is this image:\n"
        "A) A real photograph of a person or people (a selfie, headshot, portrait, candid photo, etc.)\n"
        "B) A graphic design, digital artwork, YouTube thumbnail, Twitch banner, or any other designed/edited visual\n\n"
        'Respond ONLY with the single letter "A" or "B". No other text.'
    )
    client   = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=VISION_MODEL,
        max_tokens=5,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type":      "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    answer = response.choices[0].message.content.strip().upper()
    return answer.startswith("A")


def analyze_buyer_images(image_paths: list[str]) -> dict:
    """
    Analyze all buyer-provided images with GPT-4o vision.

    Returns a merged style_context dict:
    {
      "dominant_colors": str,
      "energy_level": str,
      "layout_pattern": str,
      "aesthetic": str,
      "person_description": str | None,
      "design_notes": str,
      "has_person_photo": bool,
      "has_reference_thumbnail": bool,
      "analysis_cost": float,
    }

    Returns {} if image_paths is empty.
    """
    if not image_paths:
        return {}

    analyses: list[dict] = []
    total_cost = 0.0

    for path in image_paths:
        try:
            result, cost = _analyze_single_image(path)
            analyses.append(result)
            total_cost += cost
        except Exception as e:
            print(f"[{MODULE_NAME}]   Image analysis failed for {path}: {e}")

    if not analyses:
        return {"analysis_cost": total_cost}

    return _merge_analyses(analyses, total_cost)


def analyze_existing_thumbnail(image_path: str) -> dict:
    """
    Focused analysis of an existing YouTube thumbnail.
    Returns: {color_scheme, layout_pattern, energy_level, font_style, aesthetic, cost}
    """
    result, cost = _analyze_single_image(image_path)
    return {**result, "cost": cost}


def analyze_person_photo(image_path: str) -> dict:
    """
    Focused analysis of a person photo for prompt building.
    Returns: {person_description, dominant_colors, aesthetic, cost}
    """
    result, cost = _analyze_single_image(image_path)
    return {**result, "cost": cost}


# ─── Internal ─────────────────────────────────────────────────────────────────

def _analyze_single_image(image_path: str) -> tuple[dict, float]:
    """
    Send one image to GPT-4o vision. Returns (analysis_dict, cost_usd).
    Raises on network/API errors.
    """
    path  = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    b64     = base64.b64encode(path.read_bytes()).decode("utf-8")
    ext     = path.suffix.lstrip(".").lower()
    mime    = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"

    client   = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=VISION_MODEL,
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type":      "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "low"},
                },
                {"type": "text", "text": _ANALYSIS_PROMPT},
            ],
        }],
    )

    input_t  = response.usage.prompt_tokens
    output_t = response.usage.completion_tokens
    cost     = calc_openai_cost(VISION_MODEL, input_t, output_t)
    log_cost(MODULE_NAME, "openai", VISION_MODEL,
             tokens_used=input_t + output_t, cost_usd=cost)

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.rstrip())

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "image_type":   "reference",
            "aesthetic":    raw[:200],
            "design_notes": "See aesthetic field for raw analysis.",
        }

    return result, cost


def _merge_analyses(analyses: list[dict], total_cost: float) -> dict:
    """
    Merge multiple image analyses into a single style_context dict.
    Prioritizes thumbnail analyses for layout/colors, person photos for person_description.
    """
    thumbnails = [a for a in analyses if a.get("image_type") == "thumbnail"]
    persons    = [a for a in analyses if a.get("image_type") == "person"]
    others     = [a for a in analyses if a.get("image_type") not in ("thumbnail", "person")]

    # Pick the primary reference for colors/layout
    primary = thumbnails[0] if thumbnails else (others[0] if others else analyses[0])

    person_desc = None
    if persons:
        person_desc = persons[0].get("person_description")

    # Collect all design_notes
    all_notes = []
    for a in analyses:
        notes = a.get("design_notes", "")
        if notes:
            all_notes.append(notes)

    return {
        "dominant_colors":        primary.get("dominant_colors", ""),
        "energy_level":           primary.get("energy_level", "moderate"),
        "layout_pattern":         primary.get("layout_pattern", ""),
        "font_style":             primary.get("font_style", ""),
        "aesthetic":              primary.get("aesthetic", ""),
        "person_description":     person_desc,
        "design_notes":           " | ".join(all_notes),
        "has_person_photo":       len(persons) > 0,
        "has_reference_thumbnail": len(thumbnails) > 0,
        "analysis_cost":          total_cost,
    }


# ─── Test ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?",
                        default=str(Path(__file__).parent.parent / "assets" / "fiverr_samples" / "thumb_30days.png"),
                        help="Path to an image to analyze")
    args = parser.parse_args()

    print(f"[{MODULE_NAME}] Analyzing: {args.image}")
    result = analyze_buyer_images([args.image])
    import json as _json
    print(_json.dumps(result, indent=2))
