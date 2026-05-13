"""
publishers/fiverr_prompt_builder.py — Thumbnail prompt construction.

Inherits the shared section-assembly, niche-style-guide lookup, and revision-
feedback injection from BasePromptBuilder (core/pipeline_base.py).

FiverrPromptBuilder adds the platform-specific pieces:
  - 8 YouTube niche style guides
  - 10-section thumbnail prompt structure
  - Composition/person/output rules specific to gig delivery

All module-level functions (build_thumbnail_prompt, get_niche_style_guide,
build_background_only_prompt) remain unchanged for callers.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pipeline_base import BasePromptBuilder


# ─── Niche style guides ───────────────────────────────────────────────────────

NICHE_STYLE_GUIDES: dict[str, str] = {
    "gaming": (
        "Dark background (deep black or dark gray), neon accents (purple, cyan, or lime green). "
        "Dramatic directional lighting. Intense, high-energy composition. "
        "Large impact-font text with bright color outlines. "
        "Game-related visual elements if possible. Very saturated, high contrast. "
        "Think Markiplier, Ninja, or top-tier gaming channels."
    ),
    "finance": (
        "Clean split background (dark left side, bright green or gold right side). "
        "Professional-looking person if present, pointing or reacting with surprise/excitement. "
        "Bold white and yellow text with thick outlines. Money-related imagery (cash, charts, arrows up). "
        "Green and black or navy and gold palette. Clean, authoritative, but exciting. "
        "Think Graham Stephan, Andrei Jikh, or top finance YouTube channels."
    ),
    "fitness": (
        "High-energy, bold composition. Bright accent colors: orange, red, or electric blue on dark. "
        "Athletic or muscular person if present, with confident or intense expression. "
        "Motivational large text, possibly with a before/after split layout. "
        "High contrast, very saturated. Think transformation energy. "
        "Think Chris Heria, Jeremy Ethier, or top fitness channels."
    ),
    "food": (
        "Warm, appetizing colors: deep reds, oranges, and yellows. "
        "Close-up food visual if possible - make it look delicious and mouth-watering. "
        "Clean, readable bold text. Warm gradient background. "
        "Person reacting with delight or surprise if a face is needed. "
        "Think Binging with Babish, Joshua Weissman, or Food Ranger."
    ),
    "tech": (
        "Dark navy or deep charcoal background. Glowing UI elements, cyan or electric blue accents. "
        "Clean, modern sans-serif text. Device imagery (phone, laptop, circuit elements). "
        "Subtle glow effects and clean lines. Professional but exciting. "
        "Think MKBHD, Linus Tech Tips, or Mrwhosetheboss."
    ),
    "lifestyle": (
        "Bright, airy feel with warm tones (light beige, coral, soft yellow, pastel). "
        "Relatable, approachable person with genuine expression. "
        "Clean, readable text with plenty of breathing room. "
        "Organic, slightly editorial feel - not corporate. "
        "Think Emma Chamberlain, Bestdressed, or popular lifestyle vloggers."
    ),
    "education": (
        "Clean, trustworthy background (white, light blue, or soft gray). "
        "Clear text hierarchy - big headline, supporting visual. "
        "Simple graphic elements that illustrate the concept. "
        "Professional but approachable. No clutter. "
        "Think Kurzgesagt, Veritasium, or 3Blue1Brown."
    ),
    "default": (
        "High contrast, bold text, clear subject, professional composition. "
        "Clean background (solid or simple gradient). "
        "Eye-catching color combination with strong visual hierarchy. "
        "Click-worthy energy - make it look like a video someone would stop scrolling for."
    ),
}

# ─── Universal rules (always appended last) ───────────────────────────────────

_COMPOSITION_RULES = """\
CRITICAL COMPOSITION RULES - NON-NEGOTIABLE:

Canvas is 1536x1024. Every element must be fully inside this canvas.
Minimum 100px clear margin from ALL four edges for any text or important visual element.
Text must be 100% fully readable - no letters cut off, no words extending to the edge.
If a person is included, their full head and at least 3/4 of their body must be visible. No cropped faces.
The entire composition must feel intentionally framed, not accidentally cropped.
Generate the full scene - do not zoom in so tight that elements get cut off.\
"""

_OUTPUT_RULES = (
    "OUTPUT REQUIREMENTS: Flat artwork only. No product mockups, no frames, no borders, "
    "no phone overlays, no TV screen overlays. "
    "The design IS the thumbnail - delivered as a flat PNG. "
    "Make it indistinguishable from a real professional YouTube thumbnail."
)

_PERSON_GUIDELINES = (
    "PERSON GUIDELINES: Generate a fictional person. "
    "Do NOT generate anyone who resembles a specific real YouTuber, celebrity, or public figure. "
    "The person should look like a generic relatable content creator. "
    "Vary ethnicity, age, and appearance naturally. "
    "Never generate someone who looks like MrBeast, PewDiePie, or any recognizable creator."
)

_BACKGROUND_ONLY_NOTE = (
    "IMPORTANT: Background-only generation. A real person's photo will be composited into "
    "the empty space. Do not generate any person, face, or human figure. "
    "Leave a natural empty space on the right side approximately 40% of canvas width - "
    "it should look like a professionally designed space waiting for a person to step into."
)


# ─── Concrete prompt builder ──────────────────────────────────────────────────

class FiverrPromptBuilder(BasePromptBuilder):
    """
    Fiverr YouTube thumbnail prompt builder.

    Inherits from BasePromptBuilder:
      - get_style_guide(niche)  - fuzzy niche lookup
      - _revision_section(fb)   - standardised revision-feedback injection
      - _assemble_sections(lst) - join sections with double newline

    Adds Fiverr-specific:
      - 8 niche style guides tuned for YouTube CTR
      - Non-negotiable composition rules (100px margins, full-body visibility)
      - Person guidelines (no real-person lookalikes)
      - Background-only mode for Case A (buyer photo compositing)
    """

    niche_style_guides = NICHE_STYLE_GUIDES

    def build_prompt(
        self,
        brief:              dict,
        variation:          str | int = 0,
        rejection_feedback: str       = "",
    ) -> str:
        """
        Build a gpt-image-1 thumbnail prompt from a brief/order dict.

        ``brief`` here is the parsed Fiverr order dict.
        ``variation`` is unused (Fiverr has no angle variants).
        ``rejection_feedback`` is the QA suggested_fix from the previous attempt.
        """
        return build_thumbnail_prompt(brief, rejection_feedback=rejection_feedback)

    def build_background_only(self, order: dict, style_context: dict = None) -> str:
        """Stage-1 prompt for Case A (buyer photo compositing)."""
        return build_background_only_prompt(order, style_context)


# ─── Module-level public API (unchanged for all callers) ─────────────────────

_builder = FiverrPromptBuilder()


def get_niche_style_guide(niche: str) -> str:
    """Return the style guide for a niche, falling back to 'default'."""
    return _builder.get_style_guide(niche)


def build_background_only_prompt(
    order: dict,
    style_context: dict = None,
) -> str:
    """
    Stage-1 prompt for Case A (buyer wants their own face composited in).
    Generates background, text, and graphics only.
    """
    base = build_thumbnail_prompt(order, style_context)
    return base + "\n\n" + _BACKGROUND_ONLY_NOTE


def build_thumbnail_prompt(
    order: dict,
    style_context: dict = None,
    revision_feedback: str = None,
    background_only: bool = False,
) -> str:
    """
    Build a detailed gpt-image-1 prompt for a YouTube thumbnail.

    Args:
        order:             Parsed order dict from fiverr_parser.parse_order()
        style_context:     Optional analysis from fiverr_analyzer.analyze_buyer_images()
        revision_feedback: QA suggested_fix from the previous failed attempt
        background_only:   True for Stage 1 of the buyer-photo composite pipeline

    Returns:
        A complete prompt string ready to send to gpt-image-1.
    """
    ctx         = style_context or {}
    video_title = (order.get("video_title") or "").strip()
    niche       = (order.get("channel_niche") or "lifestyle").strip()
    style_pref  = (order.get("style_preference") or "").strip()
    has_face    = bool(order.get("has_face"))
    colors      = (order.get("color_preferences") or "").strip()
    text_req    = (order.get("text_to_include") or video_title or "").strip()
    special     = (order.get("special_instructions") or "").strip()

    niche_guide = _builder.get_style_guide(niche)

    sections: list[str] = []

    # ── 1. Core brief ─────────────────────────────────────────────────────
    sections.append(
        f"Professional YouTube thumbnail for a {niche} channel.\n"
        f"Video: \"{video_title or 'YouTube Video'}\""
    )

    # ── 2. Text requirement ───────────────────────────────────────────────
    if text_req:
        sections.append(
            f"TEXT ON THUMBNAIL (perfectly spelled, large and readable): \"{text_req}\"\n"
            "Bold, high-contrast text. Thick dark outlines for legibility on any background."
        )

    # ── 3. Niche style guide ──────────────────────────────────────────────
    sections.append(f"CHANNEL NICHE STYLE ({niche.upper()}):\n{niche_guide}")

    # ── 4. Person / face ──────────────────────────────────────────────────
    if background_only:
        sections.append(_BACKGROUND_ONLY_NOTE)
    elif has_face:
        if ctx.get("has_person_photo") and ctx.get("person_description"):
            sections.append(
                f"PERSON: Include a person matching this description - "
                f"{ctx['person_description']}. "
                "Expressive face (shocked, excited, or pointing). Place on right or left third."
            )
        else:
            sections.append(
                "PERSON: Include a person with an expressive face (shocked, excited, pointing). "
                "Approximate a young adult relevant to the channel niche. "
                "Place on the right or left third of the composition."
            )
        sections.append(_PERSON_GUIDELINES)

    # ── 5. Buyer style preferences ────────────────────────────────────────
    if style_pref:
        sections.append(f"STYLE PREFERENCE: {style_pref}")

    # ── 6. Color preferences ──────────────────────────────────────────────
    if colors:
        sections.append(f"COLOR PALETTE: Use these colors prominently: {colors}")

    # ── 7. Reference image analysis ───────────────────────────────────────
    if ctx.get("has_reference_thumbnail") and ctx.get("aesthetic"):
        sections.append(
            f"REFERENCE STYLE (match this aesthetic): {ctx['aesthetic']}"
            + (f"\nLayout: {ctx['layout_pattern']}" if ctx.get("layout_pattern") else "")
            + (f"\nColors: {ctx['dominant_colors']}" if ctx.get("dominant_colors") else "")
            + (f"\nDesign notes: {ctx['design_notes']}" if ctx.get("design_notes") else "")
        )
    elif ctx.get("aesthetic"):
        sections.append(f"VISUAL REFERENCE: {ctx['aesthetic']}")

    # ── 8. Special instructions ───────────────────────────────────────────
    if special:
        sections.append(f"SPECIAL INSTRUCTIONS: {special}")

    # ── 9. Revision feedback ──────────────────────────────────────────────
    rev = _builder._revision_section(revision_feedback or "")
    if rev:
        sections.append(rev)

    # ── 10. Universal rules (always last) ─────────────────────────────────
    sections.append(_COMPOSITION_RULES)
    sections.append(_OUTPUT_RULES)

    return _builder._assemble_sections(sections)


# ─── Test ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample_order = {
        "video_title":          "I Tried Living on $5 a Day for 30 Days",
        "channel_niche":        "finance",
        "style_preference":     "MrBeast-style, high energy, bold",
        "has_face":             True,
        "color_preferences":    "bright green and black",
        "text_to_include":      "I TRIED $5 A DAY FOR 30 DAYS",
        "special_instructions": "Make it look viral",
    }
    sample_ctx = {
        "dominant_colors":         "dark navy, bright green",
        "energy_level":            "intense",
        "layout_pattern":          "person-right-text-left",
        "aesthetic":               "Professional finance channel, clean split background",
        "has_person_photo":        False,
        "has_reference_thumbnail": False,
    }

    prompt = build_thumbnail_prompt(sample_order, sample_ctx)
    print("=" * 60)
    print(prompt)
    print("=" * 60)
    print(f"\nPrompt length: {len(prompt)} characters")

    print("\nNiche style guides:")
    for niche in NICHE_STYLE_GUIDES:
        print(f"  {niche}: {NICHE_STYLE_GUIDES[niche][:60]}...")
