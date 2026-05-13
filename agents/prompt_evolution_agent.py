"""
agents/prompt_evolution_agent.py — Self-improving prompt optimizer (spec Section 9.2)

Runs Sunday 7:30AM (America/Chicago) via scheduler/main.py.

Algorithm per variation angle:
  1. Read 7-day QA pass rate from memory.
  2. If pass rate < 0.50:
       a. Ask Claude for 3 alternative angle descriptions.
       b. Test each with 3 design+QA cycles.
       c. If winner beats current by > 15 pp: update VARIATION_ANGLES in design_agent.py.
       d. Log to memory and prompt_evolution_log.txt.
  3. Else: skip.

Returns: {"angles_updated": int, "angles_unchanged": int, "total_cost": float}

--dry-run flag:
  Skips all file writes (design_agent.py + log file) and all image generation.
  If no real underperforming angles exist in memory, simulates one so the logic
  path is fully exercised and you can see exactly what would change.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import json
import re
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

from core.supabase_client import supabase, get_latest_brief
from core.cost_logger import log_cost, calc_anthropic_cost, calc_openai_cost
from core.spend_monitor import check_cap
import core.memory_client as memory_client

# Import design_agent internals (no circular dep — evolution agent never imported by design_agent)
import agents.design_agent as design_agent

ROOT                = Path(__file__).parent.parent
AGENT_NAME          = "prompt_evolution_agent"
ANTHROPIC_MODEL     = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
LOG_FILE            = ROOT / "prompt_evolution_log.txt"
DESIGN_AGENT_PATH   = Path(__file__).parent / "design_agent.py"

PASS_RATE_THRESHOLD = 0.50     # below this triggers evaluation
IMPROVEMENT_MIN     = 0.15     # winner must beat current by this much (15 pp)
TRIALS_PER_CANDIDATE = 3       # design+QA cycles per candidate angle


# ─── Claude prompt ─────────────────────────────────────────────────────────────

EVOLUTION_PROMPT = """\
You are improving a "variation angle" prompt used by an AI image generation system that creates \
funny gift designs for Etsy print-on-demand (mugs, shirts, etc.).

A variation angle is a short creative brief — it tells the image model what emotional tone \
and concept to use for a given design attempt.

Current variation angle:
"{current_angle}"

7-day QA pass rate: {pass_rate:.0%} (target: ≥50%)
Recent rejection reasons for designs generated with this angle:
{rejection_reasons}

Diagnose why this angle underperforms, then generate 3 replacement alternatives that:
1. Keep the same broad emotional/comedic category intent
2. Are more specific and actionable — give the image model clearer direction
3. Actively avoid the patterns that caused the observed rejections
4. Work for any occupation (electricians, nurses, teachers, etc.) filled in at runtime
5. Are concise — 12 words or fewer, no filler

Return ONLY a JSON array of exactly 3 strings. No explanation, no markdown.
Example format: ["alternative one here", "alternative two here", "alternative three here"]\
"""


# ─── Recent rejections helper ──────────────────────────────────────────────────

def _get_recent_rejections(angle: str, limit: int = 5) -> list[str]:
    """
    Return recent QA rejection reasons for designs generated with a specific
    variation angle. Returns ["No rejection data available."] when the table
    lacks the variation_angle column or no rows exist.
    """
    try:
        rows = (
            supabase.table("designs")
            .select("qa_reason")
            .eq("status", "rejected")
            .eq("variation_angle", angle)
            .not_.is_("qa_reason", "null")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        reasons = [r["qa_reason"] for r in rows if r.get("qa_reason")]
        return reasons if reasons else ["No recent rejection data for this angle."]
    except Exception:
        return ["No rejection data available (column may not exist yet)."]


# ─── Candidate generation ──────────────────────────────────────────────────────

def _generate_candidates(
    current_angle: str,
    pass_rate: float,
    rejection_reasons: list[str],
) -> tuple[list[str], float]:
    """
    Ask Claude for 3 alternative angle descriptions.
    Returns (candidates, cost_usd).
    """
    reasons_text = "\n".join(f"  - {r}" for r in rejection_reasons)
    prompt = EVOLUTION_PROMPT.format(
        current_angle=current_angle,
        pass_rate=pass_rate,
        rejection_reasons=reasons_text or "  - No rejection data available.",
    )

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp   = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    input_t  = resp.usage.input_tokens
    output_t = resp.usage.output_tokens
    cost     = calc_anthropic_cost(ANTHROPIC_MODEL, input_t, output_t)

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.rstrip())

    try:
        candidates = json.loads(raw)
        if not isinstance(candidates, list):
            candidates = list(candidates.values()) if isinstance(candidates, dict) else []
        candidates = [str(c).strip() for c in candidates if c][:3]
    except json.JSONDecodeError:
        # Fallback: try to extract quoted strings
        candidates = re.findall(r'"([^"]{5,})"', raw)[:3]

    return candidates, cost


# ─── Design + QA test cycle ────────────────────────────────────────────────────

def _build_test_prompt(occupation: str, angle_text: str, keyword: str = "") -> str:
    """Build an image generation prompt for the given angle (mirrors design_agent logic)."""
    kw = keyword or occupation
    design_direction = f"{angle_text} — keyword: '{kw}'"
    return design_agent.IMAGE_PROMPT_TEMPLATE.format(
        occupation=occupation,
        design_direction=design_direction,
    )


def _test_angle(
    occupation: str,
    keyword: str,
    angle_text: str,
    n_trials: int = TRIALS_PER_CANDIDATE,
    dry_run: bool = False,
) -> tuple[float, float, list[str]]:
    """
    Run n_trials generate+QA cycles for an angle.
    Returns (pass_rate, total_cost_usd, rejection_reasons).

    In dry_run mode returns a deterministic simulated result — no API calls, no images.
    The simulated pass rate is derived from the angle text so different candidates
    produce visibly different numbers.
    """
    if dry_run:
        # Deterministic mock: hash angle text -> 0.40-0.75 band
        digest = int(hashlib.md5(angle_text.encode()).hexdigest(), 16)
        simulated = 0.40 + (digest % 100) / 200.0   # range 0.40 – 0.895
        simulated = round(min(simulated, 0.85), 2)
        return simulated, 0.0, ["(simulated — dry-run skips image generation)"]

    passes  = 0
    cost    = 0.0
    reasons = []
    prompt  = _build_test_prompt(occupation, angle_text, keyword)

    test_dir = ROOT / "designs" / "evolution-tests" / str(uuid.uuid4())[:8]
    test_dir.mkdir(parents=True, exist_ok=True)

    for trial in range(n_trials):
        try:
            image_bytes = design_agent.generate_image(prompt)
            cost       += design_agent.COST_PER_IMAGE

            # Save to temp path for QA
            img_path = test_dir / f"trial_{trial}.png"
            img_path.write_bytes(image_bytes)

            qa = design_agent.qa_check(str(img_path))
            cost += qa.get("cost", 0.0)

            if qa.get("pass"):
                passes += 1
            else:
                reasons.append(qa.get("reason", "unknown reason"))

        except Exception as e:
            print(f"    [trial {trial+1}] Error: {e}")
            reasons.append(f"generation error: {e}")

    # Clean up temp files
    try:
        import shutil
        shutil.rmtree(test_dir, ignore_errors=True)
    except Exception:
        pass

    pass_rate = passes / n_trials if n_trials else 0.0
    return pass_rate, cost, reasons


# ─── design_agent.py file update ──────────────────────────────────────────────

def _update_variation_angle(
    index: int,
    old_angle: str,
    new_angle: str,
    dry_run: bool = False,
) -> bool:
    """
    Replace old_angle with new_angle in VARIATION_ANGLES inside design_agent.py.
    Returns True if the replacement was made (or would be made in dry-run).
    """
    source = DESIGN_AGENT_PATH.read_text(encoding="utf-8")

    # Match the exact indented quoted string as it appears in the list
    old_entry = f'    "{old_angle}",'
    new_entry = f'    "{new_angle}",'

    # Verify old entry exists and is unique
    count = source.count(old_entry)
    if count == 0:
        # Try without trailing comma (last element)
        old_entry = f'    "{old_angle}"'
        new_entry = f'    "{new_angle}"'
        count = source.count(old_entry)

    if count == 0:
        print(f"  [update] Could not find angle string in design_agent.py — skipping write")
        return False
    if count > 1:
        print(f"  [update] Ambiguous match ({count} occurrences) — skipping write")
        return False

    new_source = source.replace(old_entry, new_entry, 1)

    if dry_run:
        print(f"  [DRY-RUN] Would replace in design_agent.py:")
        print(f"    OLD: {old_entry.strip()}")
        print(f"    NEW: {new_entry.strip()}")
        return True

    DESIGN_AGENT_PATH.write_text(new_source, encoding="utf-8")
    print(f"  [update] design_agent.py updated — angle {index} replaced.")
    return True


# ─── Log file ─────────────────────────────────────────────────────────────────

def _append_log(lines: list[str], dry_run: bool = False) -> None:
    """Append a block to prompt_evolution_log.txt (skip if dry-run)."""
    block = "\n".join(lines) + "\n"
    if dry_run:
        print(f"\n  [DRY-RUN] Would append to {LOG_FILE.name}:")
        for line in lines:
            print(f"    {line}")
        return
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(block)


# ─── Entry point ──────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> dict:
    """
    Evaluate every variation angle in design_agent.VARIATION_ANGLES.
    Returns {"angles_updated": int, "angles_unchanged": int, "total_cost": float}.
    """
    print(
        f"\n[{AGENT_NAME}] --- Starting "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---"
        + (" (DRY-RUN)" if dry_run else "")
    )

    if not check_cap():
        print(f"[{AGENT_NAME}] Spend cap reached — exiting.")
        return {"angles_updated": 0, "angles_unchanged": 0, "total_cost": 0.0}

    # ── Get a brief to use for test image generation ───────────────────────────
    brief = get_latest_brief()
    if brief:
        occupation = re.sub(
            r"\s*gifts?\s*$", "", brief.get("sub_niche", "electrician"), flags=re.IGNORECASE
        ).strip() or "electrician"
        keywords   = brief.get("top_keywords") or [occupation]
        keyword    = keywords[0] if keywords else occupation
    else:
        occupation, keyword = "electrician", "electrician"
        print(f"[{AGENT_NAME}]   No brief found — using 'electrician' as test occupation.")

    # ── Snapshot the current angles list from design_agent module ──────────────
    angles: list[str] = list(design_agent.VARIATION_ANGLES)
    print(f"[{AGENT_NAME}]   Evaluating {len(angles)} variation angles. "
          f"Threshold: <{PASS_RATE_THRESHOLD:.0%} pass rate.\n")

    # ── In dry-run mode, simulate at least one underperforming angle ───────────
    dry_run_overrides: dict[int, float] = {}
    if dry_run:
        all_rates = [memory_client.recall_prompt_pass_rate(a) for a in angles]
        if not any(r < PASS_RATE_THRESHOLD for r in all_rates):
            # No real underperformers — simulate angle[0] at 0.30 for demonstration
            dry_run_overrides[0] = 0.30
            print(
                f"[{AGENT_NAME}]   No underperforming angles in memory yet. "
                f"Simulating angle[0] at 0.30 pass rate for dry-run demonstration.\n"
            )

    # ── Main evaluation loop ───────────────────────────────────────────────────
    updated   = 0
    unchanged = 0
    run_cost  = 0.0

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log_lines = [
        "=" * 80,
        f"{now_str} - {AGENT_NAME}" + (" [DRY-RUN]" if dry_run else ""),
        "=" * 80,
        f"Angles evaluated: {len(angles)} | Threshold: {PASS_RATE_THRESHOLD:.0%} | "
        f"Min improvement: {IMPROVEMENT_MIN:.0%}",
        "",
    ]

    for idx, current_angle in enumerate(angles):
        # Check spend cap every iteration (generations can be expensive)
        if not check_cap():
            print(f"[{AGENT_NAME}]   Spend cap hit mid-run — stopping early.")
            break

        pass_rate = dry_run_overrides.get(idx, memory_client.recall_prompt_pass_rate(current_angle))

        angle_short = (current_angle[:55] + "...") if len(current_angle) > 55 else current_angle
        print(f"[{AGENT_NAME}]   [{idx}] \"{angle_short}\"")
        print(f"         Pass rate: {pass_rate:.0%}" + (
            " (simulated)" if idx in dry_run_overrides else "")
        )

        if pass_rate >= PASS_RATE_THRESHOLD:
            print(f"         -> above threshold, skipping.\n")
            unchanged += 1
            log_lines.append(f"[SKIP] [{idx}] \"{current_angle}\"")
            log_lines.append(f"       Pass rate: {pass_rate:.0%} - above {PASS_RATE_THRESHOLD:.0%} threshold.\n")
            continue

        # ── Get recent rejections for context ─────────────────────────────────
        recent_rejections = _get_recent_rejections(current_angle)
        print(f"         Rejections: {'; '.join(recent_rejections[:3])}")

        # ── Ask Claude for 3 alternatives ─────────────────────────────────────
        print(f"         Generating 3 candidate alternatives with Claude...")
        try:
            candidates, claude_cost = _generate_candidates(
                current_angle, pass_rate, recent_rejections
            )
            run_cost += claude_cost
            log_cost(AGENT_NAME, "anthropic", ANTHROPIC_MODEL,
                     tokens_used=0, cost_usd=claude_cost)
        except Exception as e:
            print(f"         Claude call failed: {e} — skipping this angle.\n")
            unchanged += 1
            continue

        if not candidates:
            print(f"         No valid candidates returned — skipping.\n")
            unchanged += 1
            continue

        print(f"         Candidates:")
        for i, c in enumerate(candidates):
            print(f"           [{i+1}] {c}")

        # ── Test each candidate ────────────────────────────────────────────────
        print(f"         Testing each candidate "
              f"({TRIALS_PER_CANDIDATE} {'simulated' if dry_run else 'real'} trial(s) each)...")

        results: list[tuple[str, float, float, list[str]]] = []
        for cand in candidates:
            cand_rate, cand_cost, cand_rejects = _test_angle(
                occupation, keyword, cand,
                n_trials=TRIALS_PER_CANDIDATE,
                dry_run=dry_run,
            )
            run_cost += cand_cost
            results.append((cand, cand_rate, cand_cost, cand_rejects))
            symbol = "+" if cand_rate >= pass_rate else "-"
            cand_disp = cand[:50] + "..." if len(cand) > 50 else cand
            print(f"           [{symbol}] \"{cand_disp}\" -> {cand_rate:.0%}")

        # ── Pick winner (highest pass rate) ───────────────────────────────────
        winner_angle, winner_rate, winner_cost, _ = max(results, key=lambda x: x[1])
        improvement = winner_rate - pass_rate

        direction = "^" if improvement > 0 else "v"
        winner_disp = winner_angle[:50] + "..." if len(winner_angle) > 50 else winner_angle
        print(f"         Best candidate: \"{winner_disp}\" "
              f"({winner_rate:.0%} vs current {pass_rate:.0%}, "
              f"{direction}{abs(improvement):.0%})")

        # ── Log section for this angle ─────────────────────────────────────────
        log_lines.append(f"[{'UPDATE' if improvement > IMPROVEMENT_MIN else 'NO CHANGE'}] "
                         f"[{idx}] \"{current_angle}\"")
        log_lines.append(f"       Pass rate: {pass_rate:.0%}")
        log_lines.append(f"       Candidates tested:")
        for cand, rate, _, _ in results:
            log_lines.append(f"         * \"{cand}\" -> {rate:.0%}")
        log_lines.append(f"       Winner: \"{winner_angle}\" -> {winner_rate:.0%} "
                         f"(improvement: {improvement:+.0%})")

        if improvement > IMPROVEMENT_MIN:
            # ── Write update ───────────────────────────────────────────────────
            wrote = _update_variation_angle(idx, current_angle, winner_angle, dry_run=dry_run)
            if wrote:
                updated += 1
                log_lines.append(f"       [OK] design_agent.py UPDATED"
                                  + (" (dry-run -- not written)" if dry_run else ""))
                log_lines.append("")

                # ── Update memory ──────────────────────────────────────────────
                try:
                    memory_client.remember(
                        category="prompt_performance",
                        key=f"evolution_{idx}_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                        value={
                            "angle_index":    idx,
                            "old_angle":      current_angle,
                            "new_angle":      winner_angle,
                            "old_pass_rate":  pass_rate,
                            "new_pass_rate":  winner_rate,
                            "improvement":    round(improvement, 3),
                            "dry_run":        dry_run,
                        },
                        confidence=winner_rate,
                        sample_size=TRIALS_PER_CANDIDATE,
                    )
                except Exception as e:
                    print(f"         Memory write failed: {e}")
            else:
                unchanged += 1
        else:
            unchanged += 1
            log_lines.append(
                f"       [SKIP] improvement {improvement:+.0%} < {IMPROVEMENT_MIN:.0%} "
                f"minimum — kept current angle."
            )
            log_lines.append("")

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    log_lines += [
        "-" * 80,
        f"Summary: {updated} angle(s) updated, {unchanged} unchanged. "
        f"Run cost: ${run_cost:.4f}.",
        "=" * 80,
        "",
    ]

    _append_log(log_lines, dry_run=dry_run)

    # Log total run cost to cost_logger
    if run_cost > 0:
        log_cost(AGENT_NAME, "openai", "gpt-image-1+gpt-4o",
                 tokens_used=0, cost_usd=run_cost)

    result = {
        "angles_updated": updated,
        "angles_unchanged": unchanged,
        "total_cost": round(run_cost, 4),
    }

    print(
        f"[{AGENT_NAME}] --- Done: {updated} updated, {unchanged} unchanged, "
        f"cost ${run_cost:.4f} ---\n"
    )
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Prompt Evolution Agent")
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Run full logic but skip file writes (design_agent.py + log file) "
            "and image generation. Prints what would change."
        ),
    )
    args = parser.parse_args()
    dry_run = args.dry_run
    run(dry_run=dry_run)
