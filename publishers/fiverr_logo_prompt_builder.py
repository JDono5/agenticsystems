"""
publishers/fiverr_logo_prompt_builder.py — Logo prompt construction.

Builds gpt-image-1 prompts for minimalist modern logo designs.
Follows the same BasePromptBuilder pattern as FiverrPromptBuilder.

Order dict keys used:
  business_name       — the business/brand name
  business_type       — short description of the business
  industry            — industry category (maps to INDUSTRY_STYLE_GUIDES)
  style_preferences   — buyer's style notes
  colors              — preferred color palette
  special_instructions
  revision_feedback   — QA fix from previous attempt
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pipeline_base import BasePromptBuilder


# ─── Industry style guides ────────────────────────────────────────────────────

INDUSTRY_STYLE_GUIDES: dict[str, str] = {
    "tech_startup": (
        "Geometric icon — triangle, hexagon, or circuit-inspired shape. "
        "Clean sans-serif wordmark (think Inter, Neue Haas, or Futura). "
        "Blue, dark navy, or dark charcoal palette with a single bright accent. "
        "Conveys innovation, trust, and precision."
    ),
    "restaurant": (
        "Icon incorporating a subtle food element — fork, leaf, flame, or plate. "
        "Warm inviting colors: deep red, terracotta, warm orange, or forest green. "
        "Friendly approachable font, slightly rounded. "
        "Conveys warmth, appetite, and community."
    ),
    "fitness": (
        "Bold dynamic icon — lightning bolt, abstract figure in motion, or strong geometric shape. "
        "High-contrast palette: black with electric orange, red, or lime green. "
        "Heavy bold sans-serif font with strong horizontal weight. "
        "Conveys power, energy, and transformation."
    ),
    "beauty": (
        "Elegant minimal icon — delicate leaf, abstract bloom, or thin geometric shape. "
        "Pastel, gold, blush, or ivory palette. Thin serif or luxury script font. "
        "Lots of white space. Conveys elegance, self-care, and sophistication."
    ),
    "retail": (
        "Clean recognisable icon — shopping bag silhouette, abstract mark, or letter-based monogram. "
        "Versatile palette: black/white with a pop of brand color, or clean navy. "
        "Modern rounded sans-serif. Conveys accessibility, value, and trust."
    ),
    "real_estate": (
        "Architectural or geometric icon — roof line, house outline, key, or compass mark. "
        "Navy, slate, charcoal, or warm earth tones. Professional serif or structured sans-serif. "
        "Conveys stability, authority, and professionalism."
    ),
    "creative_agency": (
        "Abstract geometric icon — bold overlapping shapes, negative-space play, or dynamic mark. "
        "Bold confident colors: electric blue, vivid purple, coral, or two contrasting hues. "
        "Modern geometric font. Conveys creativity, boldness, and vision."
    ),
    "general": (
        "Clean professional icon — simple geometric mark or letter-based monogram. "
        "Versatile palette: navy, charcoal, or black with a clean accent color. "
        "Modern sans-serif font with balanced weight. "
        "Conveys professionalism, reliability, and clarity."
    ),
}

# ─── Prompt rules (always appended) ──────────────────────────────────────────

_WHITE_BG_HEADER = """\
Minimalist modern logo design on a PURE WHITE BACKGROUND.

BACKGROUND: The background must be completely white (#FFFFFF). Absolutely no gradients anywhere
in the background. No dark areas. No glow effects. No shadows. No colored backgrounds of any kind.
No vignettes. No texture. No noise. PURE WHITE BACKGROUND ONLY. If the background is anything
other than solid white, this image fails immediately.\
"""

_LOGO_RULES = """\
DESIGN REQUIREMENTS:
- Simple clean icon or symbol relevant to the business type
- Business name in clean sans-serif typography below or beside the icon
- Flat design only — no 3D effects, no shadows, no gradients, no glow on ANY element
- 2-3 colors maximum for the logo elements themselves (the background does not count)
- The entire logo centered on the white canvas with generous equal margins on all four sides
- Nothing touching or near the edges — minimum 120px clear white space from every edge
- Vector-style precision: clean crisp edges, no rough brushwork

CRITICAL CHECKLIST — every single one of these must be true:
[ ] Background is pure solid white #FFFFFF — not off-white, not grey, not dark, not gradient
[ ] No shadows anywhere — not under text, not under icon, not cast on background
[ ] No glow effects, no bloom, no light rays, no atmospheric haze
[ ] No gradients — not in background, not in icon, not in text
[ ] Logo is perfectly centered with equal white space on all four sides
[ ] All elements fully visible, nothing cut off at any edge

This is a professional business logo for print use. Flat. Minimal. White background.\
"""

_LOGO_OUTPUT = (
    "OUTPUT: Flat vector-style logo on pure solid white background. "
    "No mockups, no product placements, no frames, no device overlays. "
    "The logo stands alone centered on white. "
    "Must look like a real professional brand logo ready for business cards and websites."
)


# ─── Concrete prompt builder ──────────────────────────────────────────────────

class LogoPromptBuilder(BasePromptBuilder):
    """
    Logo prompt builder for Fiverr logo design orders.
    Inherits get_style_guide(), _revision_section(), _assemble_sections() from BasePromptBuilder.
    """

    niche_style_guides = INDUSTRY_STYLE_GUIDES

    def build_prompt(
        self,
        order: dict,
        variation: int = 0,
        rejection_feedback: str = "",
    ) -> str:
        return build_logo_prompt(order, rejection_feedback=rejection_feedback)


# ─── Module-level public API ──────────────────────────────────────────────────

_builder = LogoPromptBuilder()


def build_logo_prompt(order: dict, rejection_feedback: str = "") -> str:
    """
    Build a gpt-image-1 prompt for a minimalist modern logo.

    Args:
        order: dict with keys: business_name, business_type, industry,
               style_preferences, colors, special_instructions
        rejection_feedback: QA suggested_fix from the previous failed attempt

    Returns:
        Complete prompt string ready for gpt-image-1.
    """
    biz_name   = (order.get("business_name") or "").strip()
    biz_type   = (order.get("business_type") or "business").strip()
    industry   = (order.get("industry") or "general").strip().lower()
    style_pref = (order.get("style_preferences") or order.get("style_preference") or "").strip()
    colors     = (order.get("colors") or order.get("color_preferences") or "").strip()
    special    = (order.get("special_instructions") or "").strip()

    industry_guide = _builder.get_style_guide(industry)

    sections: list[str] = []

    # ── 1. White background — must come first so the model sees it immediately ──
    sections.append(_WHITE_BG_HEADER)

    # ── 2. Core brief ─────────────────────────────────────────────────────
    biz_line = f"Business name: {biz_name}" if biz_name else ""
    sections.append(
        f"Business type: {biz_type}\n"
        + (f"{biz_line}\n" if biz_line else "")
        + f"Industry style: {industry_guide}"
    )

    # ── 3. Color palette ──────────────────────────────────────────────────
    if colors:
        sections.append(
            f"Color preferences: {colors}\n"
            "Use these as the logo element colors only. The background remains pure white."
        )
    else:
        sections.append(
            "Color preferences: Choose a professional palette appropriate for the industry. "
            "Maximum 3 solid colors for the logo elements. Background stays pure white."
        )

    # ── 4. Buyer style preferences ────────────────────────────────────────
    if style_pref:
        sections.append(f"Style preference: {style_pref}")

    # ── 5. Business name typography ───────────────────────────────────────
    if biz_name:
        sections.append(
            f"Include the business name '{biz_name}' in clean, readable sans-serif typography. "
            "Perfectly spelled, fully visible, legible at small sizes."
        )

    # ── 6. Special instructions ───────────────────────────────────────────
    if special:
        sections.append(f"Special instructions: {special}")

    # ── 7. Revision feedback ──────────────────────────────────────────────
    rev = _builder._revision_section(rejection_feedback)
    if rev:
        sections.append(rev)

    # ── 8. Design requirements + hard rules (always last) ─────────────────
    sections.append(_LOGO_RULES)
    sections.append(_LOGO_OUTPUT)

    return _builder._assemble_sections(sections)


# ─── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample = {
        "business_name":   "NovaBuild",
        "business_type":   "construction and renovation",
        "industry":        "real_estate",
        "style_preferences": "modern, clean, trustworthy",
        "colors":          "navy blue and white with a gold accent",
    }
    prompt = build_logo_prompt(sample)
    print("=" * 60)
    print(prompt)
    print("=" * 60)
    print(f"\nPrompt length: {len(prompt)} chars")
