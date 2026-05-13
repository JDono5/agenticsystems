import os
import sys

# Allow running as `python agents/design_agent.py` from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import openai
from dotenv import load_dotenv

from core.supabase_client import save_design, get_latest_brief
from core.cost_logger import log_cost, calc_openai_cost
from core.spend_monitor import check_cap
from core.error_handler import api_call_with_retry
from core.image_processor import process_design_background

load_dotenv()

AGENT_NAME      = "design_agent"
IMAGE_MODEL     = "gpt-image-1"
VISION_MODEL    = "gpt-4o"
TARGET_APPROVED = 8          # designs we want approved per run
MAX_ATTEMPTS    = 24         # hard ceiling (3× target) to avoid runaway spend

# gpt-image-1 high quality, 1024×1024 — $0.040/image
COST_PER_IMAGE = 0.040

# ─── Prompts ──────────────────────────────────────────────────────────────────

IMAGE_PROMPT_TEMPLATE = (
    "You are a professional graphic designer creating bestselling gift designs for Etsy. "
    "Your designs are clever, funny, and feel like something a mom would stop scrolling to buy "
    "for her husband or a friend would buy as a birthday gift.\n\n"
    "Design brief: {occupation} gift\n"
    "Concept: {design_direction}\n\n"
    "Design requirements:\n"
    "- The design must feel HUMAN made, not AI generated. "
    "Think screen print aesthetic, not clip art.\n"
    "- Include one SHORT punchy funny or relatable phrase that someone in this occupation would "
    "immediately laugh at and want. Examples of the energy we want: "
    "'Powered by Coffee and Bad Decisions', 'I Survived Another Meeting', "
    "'Electricians Do It With More Amps'. NOT generic. NOT '{occupation} Gift'. "
    "Something that makes them go 'that\\'s so me'.\n"
    "- The illustration should support the joke or phrase — not just a random tool or person.\n"
    "- Style: bold, clean, slightly vintage or hand-drawn feel. "
    "Think Redbubble bestsellers. High contrast. Works in 2-3 colors max.\n"
    "- The text must be PERFECTLY spelled, clean, and readable.\n"
    "- No extra limbs, no distorted faces, no artifacts.\n"
    "- Ask yourself: would someone actually buy this? If not, redesign it.\n\n"
    "CONSISTENCY REQUIREMENTS:\n"
    "- The entire design must fit completely within the canvas with nothing cut off at any edge.\n"
    "- Background must be solid white (#FFFFFF) with no transparency, no gradients, no textures.\n"
    "- All text must be grammatically correct English that makes logical sense when read aloud.\n"
    "- The text and illustration must relate to each other — they must tell the same joke or convey "
    "the same idea.\n"
    "- The occupation referenced must match the tools, imagery, and text used — an electrician "
    "design must have electrical imagery, not random tools.\n"
    "- Every element in the design must have a clear purpose — no random floating objects, no "
    "disconnected imagery.\n"
    "- The design must make immediate sense to someone who glances at it for 2 seconds.\n"
    "- No transparent backgrounds, no alpha channels, no PNG transparency — solid white only.\n"
    "- The complete design must be fully contained and intentionally composed — nothing should "
    "look accidental or cut off.\n\n"
    "OUTPUT REQUIREMENTS — this is the most important part:\n"
    "Flat artwork only. NO mugs, NO cups, NO product mockups, NO 3D rendered objects, "
    "NO hands holding anything, NO background scenes, NO product context whatsoever. "
    "Output just the graphic design as it would appear printed flat on a white surface. "
    "Imagine you are designing a sticker or iron-on transfer — the artwork must work as "
    "completely standalone flat art. Printify will place it on the product; your job is "
    "the graphic only. White background, centered composition, nothing else."
)

VARIATION_ANGLES = [
    "self-deprecating humor: something painfully relatable that only people in this job would get",
    "pride in the craft: celebrates mastery and expertise with a funny edge",
    "relatable daily struggle: the unglamorous reality of the job, played for laughs",
    "inside joke only this occupation gets: something outsiders wouldn't understand",
    "gifted by spouse or partner energy: 'my wife thinks I'm crazy but I love my job'",
    "retirement angle: 'survived X years of this, finally free' energy",
    "been doing this 20 years energy: grizzled veteran who has seen everything",
]

QA_PROMPT = """\
You are a brutally honest quality control reviewer for a gift mug store. Your job is to catch \
anything that would make a customer think 'something looks off' or 'this doesn't make sense'. \
You are not lenient. If something is wrong, fail it.

AUTOMATIC HARD FAILS — fail immediately on any of these:

FRAME/CROP: Any element cut off at the edge. Text or illustration that bleeds outside the canvas. \
Anything that looks like it should extend further but got cropped.
BACKGROUND: Obvious non-white background — dark backgrounds, colored backgrounds, heavy \
gradients, or clearly transparent/checkered patterns. Do NOT fail for slight off-white, cream, \
or warm white tones — these are acceptable. Only fail if the background is obviously not \
intended to be white.
TEXT NONSENSE: Text that does not form a grammatically correct English sentence or phrase. \
Gibberish words. Text that does not make logical sense when read aloud.
VISUAL MISMATCH: The illustration and text are unrelated. Example: text says electrician but \
image shows a chef. The visual must match what the text says.
OCCUPATION MISMATCH: The tools, uniform, or imagery shown do not match the occupation in the \
text. An electrician should have electrical tools not random objects.
BROKEN ANATOMY: Extra limbs, wrong number of fingers, distorted or melted faces, body parts \
that do not connect properly.
REAL PERSON RESEMBLANCE: Any person who looks like a recognizable celebrity, YouTuber, or \
public figure.
PRODUCT IN IMAGE: A mug, cup, or product mockup visible anywhere in the design.
RANDOM ELEMENTS: Objects or imagery that serve no purpose and are not connected to the design \
concept.
COMMON SENSE FAIL: If a normal person glancing at this for 2 seconds would think something is \
wrong with this — fail it. Trust your judgment.

After checking all hard fails, ask: does this look like something you would actually see for \
sale on Etsy right now? If no, fail it.

Respond ONLY with valid JSON, no markdown:
{"pass": true/false, "reason": "one specific sentence", \
"suggested_fix": "one specific instruction to fix the exact problem"}"""


# ─── Prompt building ──────────────────────────────────────────────────────────

def _extract_brief_parts(brief: dict) -> tuple[str, str, list[str]]:
    """Return (occupation, base_design_direction, top_keywords) from a brief."""
    sub_niche            = brief.get("sub_niche", "occupation")
    opportunity_summary  = brief.get("opportunity_summary", "")
    top_keywords         = brief.get("top_keywords", [])

    occupation = re.sub(r"\s*gifts?\s*$", "", sub_niche, flags=re.IGNORECASE).strip()
    if not occupation:
        occupation = sub_niche

    if "Recommended design direction:" in opportunity_summary:
        design_direction = opportunity_summary.split("Recommended design direction:")[-1].strip()
    else:
        design_direction = opportunity_summary[:200].strip()

    if not design_direction:
        design_direction = f"funny relatable quote about being a {occupation}"

    return occupation, design_direction, top_keywords


def build_prompt(brief: dict, attempt: int, rejection_reason: str = "") -> str:
    """
    Build a single image prompt for the given attempt index (0-based).

    Attempt 0 uses the brief's recommended direction.
    Subsequent attempts cycle through VARIATION_ANGLES.
    If rejection_reason is provided it is appended so the model avoids the same mistake.
    """
    occupation, base_direction, top_keywords = _extract_brief_parts(brief)

    if attempt == 0:
        design_direction = base_direction
    else:
        angle        = VARIATION_ANGLES[(attempt - 1) % len(VARIATION_ANGLES)]
        seed_keyword = top_keywords[(attempt - 1) % len(top_keywords)] if top_keywords else occupation
        design_direction = f"{angle} — keyword: '{seed_keyword}'"

    prompt = IMAGE_PROMPT_TEMPLATE.format(
        occupation=occupation,
        design_direction=design_direction,
    )

    if rejection_reason:
        prompt += (
            f"\n\nPrevious attempt was rejected for: {rejection_reason}. "
            "Avoid this in the new design."
        )

    return prompt


# ─── Image generation ─────────────────────────────────────────────────────────

def generate_image(prompt: str) -> bytes:
    """Call gpt-image-1 and return raw PNG bytes."""
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        n=1,
        size="1024x1024",
        quality="high",
    )
    return base64.b64decode(response.data[0].b64_json)


# ─── Inline QA ────────────────────────────────────────────────────────────────

def qa_check(file_path: str) -> dict:
    """
    Send the image at file_path to GPT-4o vision and return:
    { "pass": bool, "reason": str, "cost": float, "tokens": int }
    """
    image_file = Path(file_path)
    if not image_file.exists():
        return {"pass": False, "reason": f"File not found: {file_path}", "cost": 0.0, "tokens": 0}

    image_b64 = base64.b64encode(image_file.read_bytes()).decode("utf-8")
    client    = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    response = client.chat.completions.create(
        model=VISION_MODEL,
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                        "detail": "low",
                    },
                },
                {"type": "text", "text": QA_PROMPT},
            ],
        }],
    )

    input_tokens  = response.usage.prompt_tokens
    output_tokens = response.usage.completion_tokens
    cost          = calc_openai_cost(VISION_MODEL, input_tokens, output_tokens)

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.rstrip())

    result          = json.loads(raw)
    result["cost"]   = cost
    result["tokens"] = input_tokens + output_tokens
    return result


# ─── File I/O ─────────────────────────────────────────────────────────────────

def niche_slug(sub_niche: str) -> str:
    slug = sub_niche.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or "unknown-niche"


def save_to_disk(
    image_bytes: bytes,
    sub_niche: str,
    date_str: str,
    brief_id: str,
    prompt: str,
) -> tuple[str, str]:
    """Save image + sidecar JSON. Returns (file_path, image_uuid)."""
    image_uuid = str(uuid.uuid4())
    output_dir = Path("designs") / niche_slug(sub_niche) / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    image_path = output_dir / f"{image_uuid}.png"
    image_path.write_bytes(image_bytes)

    meta = {
        "brief_id": brief_id,
        "prompt_used": prompt,
        "generation_cost": COST_PER_IMAGE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / f"{image_uuid}.json").write_text(json.dumps(meta, indent=2))

    return str(image_path), image_uuid


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_design(target: int = TARGET_APPROVED, max_attempts: int = MAX_ATTEMPTS, platform: str = "etsy"):
    print(
        f"[{AGENT_NAME}] --- Starting "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---"
    )
    print(f"[{AGENT_NAME}] Target: {target} approved designs, max {max_attempts} attempts")

    if not os.getenv("OPENAI_API_KEY"):
        print(f"[{AGENT_NAME}] OPENAI_API_KEY is not set — exiting.")
        return

    if not check_cap():
        print(f"[{AGENT_NAME}] Spend cap reached — exiting.")
        return

    brief = get_latest_brief()
    if not brief:
        print(f"[{AGENT_NAME}] No research brief found — run research_agent first.")
        return

    brief_id  = brief["id"]
    sub_niche = brief.get("sub_niche", "unknown")
    print(f"[{AGENT_NAME}] Brief: {brief_id} | sub-niche: {sub_niche}")

    date_str         = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    approved         = 0
    rejected         = 0
    attempts         = 0
    total_cost       = 0.0
    last_rejection   = ""

    while approved < target and attempts < max_attempts:
        attempts += 1
        print(
            f"\n[{AGENT_NAME}] Attempt {attempts}/{max_attempts} "
            f"(approved {approved}/{target})..."
        )

        prompt = build_prompt(brief, attempts - 1, last_rejection)

        # --- Generate image ---
        image_bytes = api_call_with_retry(
            lambda p=prompt: generate_image(p),
            max_retries=3,
            agent_name=AGENT_NAME,
        )
        if not image_bytes:
            print(f"[{AGENT_NAME}]   Generation failed — skipping.")
            last_rejection = "image generation failed"
            continue

        # --- Save to disk ---
        try:
            file_path, _ = save_to_disk(image_bytes, sub_niche, date_str, brief_id, prompt)
            print(f"[{AGENT_NAME}]   Saved: {file_path}")
        except Exception as e:
            print(f"[{AGENT_NAME}]   Disk write failed: {e}")
            last_rejection = "disk write failed"
            continue

        # --- Log image generation cost (money already spent) ---
        log_cost(
            agent=AGENT_NAME,
            provider="openai",
            model=IMAGE_MODEL,
            tokens_used=0,
            cost_usd=COST_PER_IMAGE,
        )
        total_cost += COST_PER_IMAGE

        # --- Inline QA check ---
        qa_result = api_call_with_retry(
            lambda fp=file_path: qa_check(fp),
            max_retries=3,
            agent_name=AGENT_NAME,
        )
        if qa_result is None:
            print(f"[{AGENT_NAME}]   QA call failed — marking as generated for manual review.")
            save_design({
                "brief_id": brief_id,
                "file_path": file_path,
                "prompt_used": prompt,
                "generation_cost": COST_PER_IMAGE,
                "status": "generated",
                "platform": platform,
                "niche": sub_niche,
            })
            last_rejection = "QA call failed"
            continue

        qa_cost   = qa_result.get("cost", 0.0)
        qa_tokens = qa_result.get("tokens", 0)
        passed_qa = bool(qa_result.get("pass", False))
        reason    = qa_result.get("reason", "")

        log_cost(
            agent=AGENT_NAME,
            provider="openai",
            model=VISION_MODEL,
            tokens_used=qa_tokens,
            cost_usd=qa_cost,
        )
        total_cost += qa_cost

        # Remove white background on approved designs so Printify gets a
        # transparent PNG that works on any product colour.  QA already ran on
        # the original white-background version so the background check stays valid.
        if passed_qa:
            try:
                process_design_background(file_path)
                print(f"[{AGENT_NAME}]   Background removed (transparent PNG ready for Printify)")
            except Exception as bg_err:
                print(f"[{AGENT_NAME}]   Background removal failed (continuing): {bg_err}")

        final_status = "approved" if passed_qa else "rejected"
        save_design({
            "brief_id": brief_id,
            "file_path": file_path,
            "prompt_used": prompt,
            "generation_cost": COST_PER_IMAGE,
            "status": final_status,
            "qa_reason": reason,
            "platform": platform,
            "niche": sub_niche,
        })

        verdict = "PASS" if passed_qa else "FAIL"
        print(f"[{AGENT_NAME}]   {verdict} — {reason} (img ${COST_PER_IMAGE:.3f} + qa ${qa_cost:.6f})")

        if passed_qa:
            approved += 1
            last_rejection = ""
        else:
            rejected += 1
            # Prefer suggested_fix for the next attempt — it's more actionable
            last_rejection = qa_result.get("suggested_fix") or reason

    # --- Summary ---
    exhausted = attempts >= max_attempts and approved < target
    print(
        f"\n[{AGENT_NAME}] --- Run complete ---\n"
        f"[{AGENT_NAME}]   Approved : {approved}\n"
        f"[{AGENT_NAME}]   Rejected : {rejected}\n"
        f"[{AGENT_NAME}]   Attempts : {attempts}/{max_attempts}\n"
        f"[{AGENT_NAME}]   Cost     : ${total_cost:.4f}\n"
        + (f"[{AGENT_NAME}]   WARNING  : hit attempt ceiling before reaching {target} approvals"
           if exhausted else "")
    )


# Scheduler-compatible entry point (spec Section 6.2 / 10)
def run(platform: str = "etsy", target: int = TARGET_APPROVED, max_attempts: int = MAX_ATTEMPTS):
    run_design(target=target, max_attempts=max_attempts)


if __name__ == "__main__":
    if "--full" in sys.argv:
        run_design()
    else:
        print(f"[{AGENT_NAME}] Single-image test (pass --full to run full pipeline)")
        run_design(target=1, max_attempts=3)
