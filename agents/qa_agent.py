"""
agents/qa_agent.py — Etsy print-on-demand design QA (spec Section 5.2 QA step)

Inherits the shared GPT-4o vision pipeline from BaseQA (core/pipeline_base.py).
EtsyQA adds the mug/POD-specific criteria: flat artwork, no product mockups,
occupation relevance, commercial viability.

The module-level ``evaluate_design`` and ``run_qa`` functions are the public
API — callers (design_agent, scheduler) never need to know about the class.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from core.pipeline_base import BaseQA
from core.supabase_client import get_designs_by_status, update_design_status
from core.spend_monitor import check_cap
from core.error_handler import api_call_with_retry

load_dotenv()

AGENT_NAME = "qa_agent"

# ─── Etsy POD evaluation prompt ───────────────────────────────────────────────
# No system prompt needed — the prompt itself is fully self-contained.

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
"suggested_fix": "one specific instruction to fix the exact problem"}\
"""


# ─── Concrete QA class ────────────────────────────────────────────────────────

class EtsyQA(BaseQA):
    """
    Etsy print-on-demand design QA.

    The Etsy QA prompt is stateless — it does not need to extract fields from
    the context dict because the same 7 criteria apply to every design.
    The ``context`` arg (the design dict) is accepted but ignored in
    ``build_prompt``; it is the image itself that carries all the information.
    """

    platform      = AGENT_NAME
    system_prompt = None  # self-contained prompt; no separate system message

    def build_prompt(self, context: dict) -> str:
        """Return the fixed Etsy POD criteria prompt (context is ignored)."""
        return QA_PROMPT


# ─── Module-level public API (unchanged signatures for all callers) ─────────────

_qa = EtsyQA()


def evaluate_design(design: dict) -> dict:
    """
    Send a design image to GPT-4o vision and return a QA result dict.

    Args:
        design: Design record with at minimum a "file_path" key.

    Returns:
        {"pass": bool, "reason": str, "suggested_fix": str|None,
         "cost": float, "tokens": int}
    """
    return _qa.qa_check(design.get("file_path", ""), design)


# ─── Batch runner (invoked by scheduler / run directly) ──────────────────────

def run_qa() -> None:
    print(
        f"[{AGENT_NAME}] --- Starting "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---"
    )

    if not os.getenv("OPENAI_API_KEY"):
        print(f"[{AGENT_NAME}] OPENAI_API_KEY is not set - exiting.")
        return

    if not check_cap():
        print(f"[{AGENT_NAME}] Spend cap reached - exiting.")
        return

    pending = get_designs_by_status("generated")
    if not pending:
        print(f"[{AGENT_NAME}] No designs with status 'generated' - nothing to review.")
        return

    print(f"[{AGENT_NAME}] Reviewing {len(pending)} design(s)...")

    passed = failed = errors = 0

    for design in pending:
        design_id = design["id"]
        file_path = design.get("file_path", "unknown")
        print(f"\n[{AGENT_NAME}] Evaluating {design_id[:8]}... ({Path(file_path).name})")

        result = api_call_with_retry(
            lambda d=design: evaluate_design(d),
            max_retries=3,
            agent_name=AGENT_NAME,
        )

        if result is None:
            print(f"[{AGENT_NAME}]   Vision call failed - leaving as 'generated'.")
            errors += 1
            continue

        passed_qa = bool(result.get("pass", False))
        reason    = result.get("reason", "")
        cost      = result.get("cost", 0.0)

        new_status = "approved" if passed_qa else "rejected"
        api_call_with_retry(
            lambda did=design_id, s=new_status, r=reason: update_design_status(did, s, r),
            max_retries=3,
            agent_name=AGENT_NAME,
        )

        verdict_label = "PASS" if passed_qa else "FAIL"
        print(f"[{AGENT_NAME}]   {verdict_label} - {reason} (${cost:.6f})")

        if passed_qa:
            passed += 1
        else:
            failed += 1

    print(
        f"\n[{AGENT_NAME}] --- Run complete: "
        f"{passed} approved, {failed} rejected, {errors} errors ---"
    )


run = run_qa


if __name__ == "__main__":
    run_qa()
