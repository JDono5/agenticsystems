"""
publishers/fiverr_social_prompt_builder.py — Instagram social media post prompt construction.

Builds gpt-image-1 prompts for 1024x1024 (Instagram-ready) social media graphics.
Follows the same BasePromptBuilder pattern used across the pipeline.

Order dict keys used:
  business_name       — the brand/business name
  business_type       — short description of the business
  colors              — brand colors
  style               — visual style notes
  num_posts           — total posts being ordered (used to vary themes)
  special_instructions
  revision_feedback   — QA fix from previous attempt

post_index is passed into build_prompt so each post in a batch gets a distinct theme.
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pipeline_base import BasePromptBuilder


# ─── Post theme generator ─────────────────────────────────────────────────────

# Five rotating theme families. For a batch, posts cycle through these so no
# two consecutive posts share the same theme.
_THEME_FAMILIES = [
    "promotional",
    "inspirational_quote",
    "product_highlight",
    "behind_the_scenes",
    "tips_and_tricks",
]

_THEME_DETAILS: dict[str, dict] = {
    "promotional": {
        "label":   "Promotional Offer",
        "headline_hint": "Limited time offer or sale announcement",
        "subtext_hint":  "Call to action — 'Shop now', 'DM to order', 'Link in bio'",
        "vibe":    "bold, high contrast, urgency — large price or discount prominently displayed",
    },
    "inspirational_quote": {
        "label":   "Inspirational Quote",
        "headline_hint": "Short powerful motivational or brand-aligned quote",
        "subtext_hint":  "Brand name or tagline attribution",
        "vibe":    "clean, minimal, emotional — quote as the hero element with elegant typography",
    },
    "product_highlight": {
        "label":   "Product or Service Highlight",
        "headline_hint": "Feature or benefit of the main product/service",
        "subtext_hint":  "One-line description or key selling point",
        "vibe":    "clean product-focused layout, benefit-driven headline, clear visual hierarchy",
    },
    "behind_the_scenes": {
        "label":   "Behind the Scenes",
        "headline_hint": "Personal or process-focused story headline",
        "subtext_hint":  "Invite engagement — 'Follow our journey', 'Ask us anything'",
        "vibe":    "warm, authentic, slightly less polished — human and relatable",
    },
    "tips_and_tricks": {
        "label":   "Tips and Tricks",
        "headline_hint": "Numbered tip or 'How to' headline",
        "subtext_hint":  "Brief explanation or list item",
        "vibe":    "educational, clean layout, icon-accented, trustworthy",
    },
}


def generate_post_theme(order: dict, post_index: int) -> dict:
    """
    Return a theme dict for a given post index in the batch.
    Cycles deterministically through _THEME_FAMILIES so a 5-post batch
    covers all five themes.

    Returns:
        {
          "family":         str,
          "label":          str,
          "headline_hint":  str,
          "subtext_hint":   str,
          "vibe":           str,
        }
    """
    family = _THEME_FAMILIES[post_index % len(_THEME_FAMILIES)]
    details = _THEME_DETAILS[family]
    return {
        "family": family,
        "label":  details["label"],
        "headline_hint": details["headline_hint"],
        "subtext_hint":  details["subtext_hint"],
        "vibe":          details["vibe"],
    }


# ─── Prompt rules (always appended) ──────────────────────────────────────────

_SOCIAL_RULES = """\
CRITICAL SOCIAL MEDIA RULES — NON-NEGOTIABLE:

Canvas is 1024x1024 square (Instagram post format). All elements must be fully inside.
Minimum 60px clear margin from ALL four edges for any text or important graphic element.
All text must be 100% readable — no letters cut off, no words touching any edge.
Strong visual hierarchy: one dominant headline, one supporting element — not equal weight.
Clean modern layout: avoid clutter, maximum 2-3 text lines total.
White or brand-colored solid background — no gradients unless very subtle, no noise textures.
No blur, no cut-off elements, no partial graphics crossing the edges.
Professional quality suitable for posting directly to Instagram without any edits.\
"""

_SOCIAL_OUTPUT = (
    "OUTPUT: Flat 1024x1024 social media graphic. "
    "No phone mockups, no device frames, no Instagram UI overlays. "
    "The graphic IS the post — delivered as a clean flat PNG. "
    "Must look like a real professional brand's Instagram post."
)


# ─── Concrete prompt builder ──────────────────────────────────────────────────

class SocialPromptBuilder(BasePromptBuilder):
    """
    Social media post prompt builder for Fiverr Instagram design orders.
    Inherits get_style_guide(), _revision_section(), _assemble_sections() from BasePromptBuilder.
    """

    niche_style_guides = {}  # No niche guides — theme system replaces this

    def build_prompt(
        self,
        order: dict,
        post_index: int = 0,
        rejection_feedback: str = "",
    ) -> str:
        return build_social_prompt(order, post_index, rejection_feedback=rejection_feedback)


# ─── Module-level public API ──────────────────────────────────────────────────

_builder = SocialPromptBuilder()


def build_social_prompt(
    order: dict,
    post_index: int = 0,
    rejection_feedback: str = "",
) -> str:
    """
    Build a gpt-image-1 prompt for an Instagram post graphic.

    Args:
        order:             dict with keys: business_name, business_type, colors,
                           style, special_instructions
        post_index:        0-based index in the batch — controls post theme variation
        rejection_feedback: QA suggested_fix from previous failed attempt

    Returns:
        Complete prompt string ready for gpt-image-1.
    """
    biz_name   = (order.get("business_name") or "").strip()
    biz_type   = (order.get("business_type") or "business").strip()
    colors     = (order.get("colors") or order.get("color_preferences") or "").strip()
    style      = (order.get("style") or order.get("style_preference") or "clean modern").strip()
    special    = (order.get("special_instructions") or "").strip()

    theme = generate_post_theme(order, post_index)

    sections: list[str] = []

    # ── 1. Core brief ─────────────────────────────────────────────────────
    sections.append(
        f"Instagram post graphic for {biz_name + ', ' if biz_name else ''}a {biz_type} business.\n"
        f"Post theme: {theme['label']}."
    )

    # ── 2. Visual vibe for this theme ─────────────────────────────────────
    sections.append(f"VISUAL STYLE FOR THIS POST: {theme['vibe']}")

    # ── 3. Headline and subtext ───────────────────────────────────────────
    sections.append(
        f"HEADLINE TEXT: {theme['headline_hint']}. "
        f"Bold, large, high-contrast — the first thing the eye lands on.\n"
        f"SUPPORTING TEXT: {theme['subtext_hint']}. "
        "Smaller, secondary weight — supports the headline without competing."
    )

    # ── 4. Brand colors ───────────────────────────────────────────────────
    if colors:
        sections.append(
            f"BRAND COLORS: {colors}. Use these as the primary palette. "
            "Background should be white or one of these brand colors (solid, clean)."
        )
    else:
        sections.append(
            "COLOR PALETTE: Choose a clean professional palette — "
            "white or light background with 1-2 accent colors, solid fills only."
        )

    # ── 5. Overall style ──────────────────────────────────────────────────
    sections.append(f"OVERALL STYLE: {style}")

    # ── 6. Business name branding ─────────────────────────────────────────
    if biz_name:
        sections.append(
            f"BRANDING: Include '{biz_name}' as a subtle watermark or brand footer — "
            "smaller than the headline, placed at bottom or top edge (with margin)."
        )

    # ── 7. Special instructions ───────────────────────────────────────────
    if special:
        sections.append(f"SPECIAL INSTRUCTIONS: {special}")

    # ── 8. Revision feedback ──────────────────────────────────────────────
    rev = _builder._revision_section(rejection_feedback)
    if rev:
        sections.append(rev)

    # ── 9. Hard rules (always last) ───────────────────────────────────────
    sections.append(_SOCIAL_RULES)
    sections.append(_SOCIAL_OUTPUT)

    return _builder._assemble_sections(sections)


# ─── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample = {
        "business_name":  "Bloom Bakery",
        "business_type":  "artisan bakery",
        "colors":         "pastel pink and cream with gold accents",
        "style":          "elegant, warm, Instagram-aesthetic",
    }
    print("Generating 5-post batch themes:")
    for i in range(5):
        theme = generate_post_theme(sample, i)
        prompt = build_social_prompt(sample, post_index=i)
        print(f"\n--- Post {i+1}: {theme['label']} ---")
        print(prompt[:400] + "...\n")
        print(f"Prompt length: {len(prompt)} chars")
