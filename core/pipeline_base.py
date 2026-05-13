"""
core/pipeline_base.py — Abstract base classes for the AI Agent System pipeline.

PURPOSE
-------
Every income stream follows the same five patterns:
  QA        – vision-check a generated image and return pass/fail with a fix hint
  Prompt    – assemble a generation prompt from a brief/order + style context
  Publisher – deliver a finished asset to the platform and verify credentials
  Learning  – write outcomes to memory and read best/worst patterns back
  Scout     – scrape opportunities, evaluate with Claude, save qualifying proposals

These classes encode the shared logic once. Adding a new income stream means:
  1. Subclass the relevant base classes.
  2. Fill in the platform-specific pieces (prompts, key names, scrapers).
  3. Write thin module-level wrappers so the scheduler/tests keep working.

If done right, stream #3 should take roughly half the time of stream #2.

HOW TO ADD A NEW STREAM (e.g. Amazon KDP)
------------------------------------------
  # publishers/kdp_qa.py
  from core.pipeline_base import BaseQA

  class KDPQA(BaseQA):
      platform     = "kdp"
      system_prompt = "You are a quality reviewer for Amazon KDP low-content books..."

      def build_prompt(self, context: dict) -> str:
          # describe the KDP-specific checks using context["title"], context["category"] etc.
          return f"Evaluate this book cover for {context.get('category')} ..."

  def qa_cover(image_path: str, order: dict) -> dict:
      return KDPQA().qa_check(image_path, order)


  # publishers/kdp_learning.py
  from core.pipeline_base import BaseLearning

  class KDPLearning(BaseLearning):
      platform = "kdp"

  _learning = KDPLearning()
  def log_order_to_memory(order, prompt, ctx): _learning.log_success("kdp", order["niche"], prompt, ctx)
  def get_high_performing_patterns(niche):     return _learning.get_best_patterns("kdp", niche)

Everything else (Anthropic eval loop, Supabase reads, cost logging) is already handled.
"""

from __future__ import annotations

import base64
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
import anthropic

import core.memory_client as memory_client
from core.cost_logger import log_cost, calc_openai_cost, calc_anthropic_cost
from core.spend_monitor import check_cap
from core.error_handler import api_call_with_retry
from core.supabase_client import supabase, save_proposal

VISION_MODEL    = "gpt-4o"
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")


# ══════════════════════════════════════════════════════════════════════════════
#  BaseQA
# ══════════════════════════════════════════════════════════════════════════════

class BaseQA(ABC):
    """
    Shared GPT-4o vision QA pipeline.

    The only things that differ between platforms are:
      - The optional system prompt (sets the reviewer's persona)
      - The per-image evaluation prompt (built from the context dict)

    Everything else — base64 encoding, the API call, JSON parsing, markdown
    stripping, cost logging — lives here and never needs to be re-written.

    Subclass contract
    -----------------
    platform     : str          – used as the cost-log agent name
    system_prompt: str | None   – reviewer persona (None = no system message)
    build_prompt (context) -> str  – assemble the evaluation question for THIS image
    """

    platform:      str       = "unknown"
    system_prompt: str | None = None

    @abstractmethod
    def build_prompt(self, context: dict) -> str:
        """
        Build the per-image evaluation prompt from the context dict.

        ``context`` is whatever the caller passes — an order dict for Fiverr,
        a design dict for Etsy, etc.  The method should extract what it needs
        and construct a clear, criteria-numbered prompt that ends with:

            Respond ONLY with valid JSON (no markdown):
            {"pass": true/false, "reason": "...", "suggested_fix": "..."|null}
        """

    # ── Public entry point ──────────────────────────────────────────────────

    def qa_check(self, image_path: str, context: dict) -> dict:
        """
        Evaluate *image_path* against *context* and return::

            {
              "pass":          bool,
              "reason":        str,
              "suggested_fix": str | None,
              "cost":          float,
              "tokens":        int,
            }

        Never raises — returns a failing result on any error.
        """
        path = Path(image_path)
        if not path.exists():
            return {
                "pass":          False,
                "reason":        f"QA skipped - file not found: {image_path}",
                "suggested_fix": "Ensure the image was saved before running QA.",
                "cost":          0.0,
                "tokens":        0,
            }

        b64          = base64.b64encode(path.read_bytes()).decode("utf-8")
        eval_prompt  = self.build_prompt(context)

        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({
            "role": "user",
            "content": [
                {
                    "type":      "image_url",
                    "image_url": {
                        "url":    f"data:image/png;base64,{b64}",
                        "detail": "low",
                    },
                },
                {"type": "text", "text": eval_prompt},
            ],
        })

        client   = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model=VISION_MODEL,
            max_tokens=300,
            messages=messages,
        )

        input_t  = response.usage.prompt_tokens
        output_t = response.usage.completion_tokens
        cost     = calc_openai_cost(VISION_MODEL, input_t, output_t)
        log_cost(
            agent=self.platform,
            provider="openai",
            model=VISION_MODEL,
            tokens_used=input_t + output_t,
            cost_usd=cost,
        )

        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$",         "", raw.rstrip())

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {
                "pass":          False,
                "reason":        "QA response could not be parsed - treating as fail",
                "suggested_fix": "Regenerate with same prompt.",
            }

        result.setdefault("suggested_fix", None)
        result["cost"]   = cost
        result["tokens"] = input_t + output_t
        return result


# ══════════════════════════════════════════════════════════════════════════════
#  BasePromptBuilder
# ══════════════════════════════════════════════════════════════════════════════

class BasePromptBuilder(ABC):
    """
    Shared prompt-construction utilities.

    All platforms maintain a dict of niche style guides, need revision-feedback
    injection, and assemble prompts from ordered sections.  This class provides
    those three capabilities so subclasses never duplicate them.

    Subclass contract
    -----------------
    niche_style_guides : dict[str, str]
        Map of niche name -> style description.  Must include a "default" key.

    build_prompt(brief, variation, rejection_feedback) -> str
        Assemble the full generation prompt. Use ``get_style_guide``,
        ``_revision_section``, and ``_assemble_sections`` as building blocks.
    """

    niche_style_guides: dict[str, str] = {"default": "High-contrast, professional, eye-catching."}

    @abstractmethod
    def build_prompt(
        self,
        brief:             dict,
        variation:         str | int = 0,
        rejection_feedback: str      = "",
    ) -> str:
        """Return the complete image-generation prompt string."""

    # ── Shared helpers ──────────────────────────────────────────────────────

    def get_style_guide(self, niche: str) -> str:
        """
        Fuzzy-match *niche* against ``niche_style_guides`` keys.

        Matching rules (in order):
          1. Exact key match
          2. Key is a substring of niche (e.g. "finance" in "personal finance")
          3. Niche is a substring of key
          4. Fall back to "default"
        """
        key = (niche or "").lower().strip()
        if key in self.niche_style_guides:
            return self.niche_style_guides[key]
        for known in self.niche_style_guides:
            if known != "default" and (known in key or key in known):
                return self.niche_style_guides[known]
        return self.niche_style_guides.get("default", "")

    def _revision_section(self, feedback: str) -> str:
        """Return a standardised revision-feedback section, or '' if no feedback."""
        if not feedback:
            return ""
        return (
            "REVISION NOTE - the previous version was rejected because:\n"
            f'"{feedback}"\n'
            "Address this specific issue directly. Do NOT repeat the same mistake."
        )

    def _assemble_sections(self, sections: list[str]) -> str:
        """Join non-empty sections with a double newline separator."""
        return "\n\n".join(s.strip() for s in sections if s and s.strip())


# ══════════════════════════════════════════════════════════════════════════════
#  BasePublisher
# ══════════════════════════════════════════════════════════════════════════════

class BasePublisher(ABC):
    """
    Contract every publisher module must satisfy.

    ``publish`` is the only thing the orchestrator and scheduler need to call.
    ``verify_credentials`` lets the orchestrator surface missing keys to the
    owner before a run attempt rather than failing mid-delivery.

    Subclass contract
    -----------------
    platform             : str
    publish(design, listing_copy, config) -> dict
        Deliver the asset, return a result dict with at least:
        {"success": bool, "url": str | None, "cost": float, "error": str | None}
    verify_credentials() -> bool
        Return True iff all required env vars / API keys are present and valid.
    """

    platform: str = "unknown"

    @abstractmethod
    def publish(
        self,
        design:       dict,
        listing_copy: dict,
        config:       dict,
    ) -> dict:
        """
        Deliver a finished asset to the platform.

        Args:
            design:       Design record (file_path, niche, id, …)
            listing_copy: Title, tags, description generated by the publisher agent
            config:       Platform config from platform_config/<platform>.json

        Returns:
            {"success": bool, "url": str | None, "cost": float, "error": str | None}
        """

    @abstractmethod
    def verify_credentials(self) -> bool:
        """Return True iff all required credentials are present and reachable."""

    # ── Shared helpers every publisher can use ──────────────────────────────

    def _check_spend_cap(self) -> bool:
        """True = budget available, False = skip this run."""
        return check_cap()

    def _log_cost(
        self,
        provider: str,
        model:    str,
        tokens:   int,
        cost_usd: float,
    ) -> None:
        log_cost(
            agent=self.platform,
            provider=provider,
            model=model,
            tokens_used=tokens,
            cost_usd=cost_usd,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  BaseLearning
# ══════════════════════════════════════════════════════════════════════════════

class BaseLearning(ABC):
    """
    Shared memory read/write for the learning feedback loop.

    Every platform needs to:
      - Record what happened after each successful delivery
      - Record negative outcomes (low rating, rejection)
      - Retrieve the best-performing patterns before building the next prompt

    The key-naming convention, the rolling-list append logic, and the memory
    read helpers are identical across platforms — they live here.

    Subclass contract
    -----------------
    platform              : str   e.g. "fiverr", "etsy", "kdp"
    memory_category_orders: str   allowed category for order records
    memory_category_patterns: str allowed category for pattern records
    max_stored            : int   rolling window size (default 10)
    """

    platform:               str = "unknown"
    memory_category_orders: str = "platform_health"
    memory_category_patterns: str = "prompt_performance"
    max_stored:             int = 10

    # ── Generic interface (use these from orchestrator / memory_agent) ──────

    def log_success(
        self,
        niche:   str,
        prompt:  str,
        context: dict,
        extra:   dict | None = None,
    ) -> None:
        """
        Record a successful outcome for *niche* + *prompt*.

        Writes to memory key ``{platform}_{niche}_style_notes`` under
        ``memory_category_orders``.
        """
        niche_key = niche.lower().replace(" ", "_")
        key       = f"{self.platform}_{niche_key}_style_notes"
        existing  = memory_client.recall(key)
        entries   = []
        if existing:
            entries = existing.get("value", {}).get("orders", [])

        entry: dict = {
            "prompt":    prompt[:500],
            "context":   {k: v for k, v in (context or {}).items() if k != "analysis_cost"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            entry.update(extra)

        entries = entries[-(self.max_stored - 1):] + [entry]
        try:
            memory_client.remember(
                category=self.memory_category_orders,
                key=key,
                value={"niche": niche_key, "orders": entries},
                confidence=0.7,
                sample_size=len(entries),
            )
        except Exception as e:
            print(f"[{self.__class__.__name__}] log_success write failed: {e}")

    def log_failure(
        self,
        niche:  str,
        prompt: str,
        reason: str,
        rating: int = 1,
    ) -> None:
        """Append *prompt* to the underperforming-patterns list for *niche*."""
        niche_key = niche.lower().replace(" ", "_")
        self._append_pattern(
            f"{self.platform}_{niche_key}_underperforming",
            prompt,
            reason,
            rating,
        )

    def get_best_patterns(self, niche: str) -> list[str]:
        """Return prompts from 5-star / high-performing outcomes for *niche*."""
        return self._get_patterns(
            f"{self.platform}_{niche.lower().replace(' ', '_')}_high_performing"
        )

    def get_worst_patterns(self, niche: str) -> list[str]:
        """Return prompts from low-rated / rejected outcomes for *niche*."""
        return self._get_patterns(
            f"{self.platform}_{niche.lower().replace(' ', '_')}_underperforming"
        )

    # ── Shared internal helpers ─────────────────────────────────────────────

    def _append_pattern(
        self,
        key:    str,
        prompt: str,
        review: str,
        rating: int,
    ) -> None:
        """
        Append one entry to a rolling pattern list in memory.

        The list is capped at ``max_stored`` entries (oldest dropped first).
        Category used: ``memory_category_patterns``.
        """
        try:
            existing = memory_client.recall(key)
            patterns: list[dict] = []
            if existing:
                patterns = existing.get("value", {}).get("patterns", [])

            entry = {
                "prompt":    prompt[:500],
                "review":    review[:200],
                "rating":    rating,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            patterns = patterns[-(self.max_stored - 1):] + [entry]

            memory_client.remember(
                category=self.memory_category_patterns,
                key=key,
                value={"patterns": patterns},
                confidence=float(max(rating, 1)) / 5.0,
                sample_size=len(patterns),
            )
        except Exception as e:
            print(f"[{self.__class__.__name__}] _append_pattern({key}) failed: {e}")

    def _get_patterns(self, key: str) -> list[str]:
        """Return list of prompt strings from a pattern memory key."""
        row = memory_client.recall(key)
        if not row:
            return []
        patterns = row.get("value", {}).get("patterns", [])
        return [p.get("prompt", "") for p in patterns if p.get("prompt")]


# ══════════════════════════════════════════════════════════════════════════════
#  BaseScout
# ══════════════════════════════════════════════════════════════════════════════

class BaseScout(ABC):
    """
    Shared opportunity-scouting pipeline.

    Flow every scout runs:
      1. Spend-cap check
      2. scrape_opportunities()           ← platform-specific
      3. _get_rejected_history()          ← shared (queries scout_proposals)
      4. _evaluate_with_claude(raw, rej)  ← shared (calls Claude, parses JSON)
      5. _save_proposals(proposals)       ← shared (supabase upsert)
      6. _on_proposals_saved(saved)       ← hook (default: no-op)

    Subclass contract
    -----------------
    platform : str
        Stored in ``scout_proposals.platform`` — used for rejected-history
        filtering.  e.g. "etsy", "fiverr_expansion".
    max_proposals : int
        Cap on proposals saved per run (default 3).
    scrape_opportunities() -> Any
        Gather raw market data.  May return a list[dict], a str, or any JSON-
        serialisable object.  Passed as-is to _build_evaluation_prompt.
    _build_evaluation_prompt(raw_data, rejected) -> str
        Format the full Claude prompt from the scraped data + rejected history.
    """

    platform:      str = "unknown"
    max_proposals: int = 3
    module_name:   str = "base_scout"

    @abstractmethod
    def scrape_opportunities(self) -> Any:
        """
        Collect raw market / gig data for this platform.

        The return value is passed directly to ``_build_evaluation_prompt`` so
        the format is entirely up to the subclass.  Common return types:
          - str  — for scouts that pre-format a text summary
          - list[dict] — for scouts that return structured gig records
        """

    @abstractmethod
    def _build_evaluation_prompt(
        self,
        raw_data:         Any,
        rejected_history: list[str],
    ) -> str:
        """
        Build the full Claude prompt from scraped data + rejected history.

        The prompt must instruct Claude to return one JSON object per line
        (or a JSON array) with at minimum these keys:
          - opportunity_name
          - platform
          - how_it_works
          - monthly_potential (int, USD)
        """

    # ── Optional hook ───────────────────────────────────────────────────────

    def _on_proposals_saved(self, saved: list[dict]) -> None:
        """
        Called after proposals are written to Supabase.

        Override to e.g. enqueue a job, send a notification, or update a
        dashboard counter.  Default is a no-op.
        """

    # ── Shared orchestration ────────────────────────────────────────────────

    def run(self) -> list[dict]:
        """
        Execute the full scout cycle and return the list of saved proposals.

        Safe to call from the scheduler — never raises; returns [] on any
        top-level failure.
        """
        if not check_cap():
            print(f"[{self.module_name}] Monthly spend cap hit - skipping")
            return []

        print(
            f"[{self.module_name}] --- Starting "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ---"
        )

        raw_data = api_call_with_retry(
            self.scrape_opportunities,
            max_retries=2,
            agent_name=self.module_name,
        )
        if raw_data is None:
            print(f"[{self.module_name}]   Scraping returned no data - aborting")
            return []
        print(f"[{self.module_name}]   Scraping complete")

        rejected = self._get_rejected_history()
        print(f"[{self.module_name}]   Rejected history: {len(rejected)} entries")

        proposals = api_call_with_retry(
            lambda: self._evaluate_with_claude(raw_data, rejected),
            max_retries=2,
            agent_name=self.module_name,
        ) or []
        print(f"[{self.module_name}]   Claude identified {len(proposals)} qualifying opportunity(-ies)")

        saved = self._save_proposals(proposals)
        print(f"[{self.module_name}] --- Done: {len(saved)} proposal(s) saved ---")

        self._on_proposals_saved(saved)
        return saved

    def _get_rejected_history(self) -> list[str]:
        """Return names of previously ignored proposals for this platform."""
        try:
            rows = (
                supabase.table("scout_proposals")
                .select("opportunity_name")
                .eq("platform", self.platform)
                .eq("status", "ignored")
                .execute()
                .data
            )
            return [r["opportunity_name"] for r in rows]
        except Exception:
            return []

    def _evaluate_with_claude(
        self,
        raw_data:         Any,
        rejected_history: list[str],
    ) -> list[dict]:
        """
        Send the evaluation prompt to Claude and parse the response.

        Handles both response formats:
          - One JSON object per line (fiverr_scout style)
          - A JSON array (scout_agent style)
        """
        prompt = self._build_evaluation_prompt(raw_data, rejected_history)

        client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        input_t  = response.usage.input_tokens
        output_t = response.usage.output_tokens
        cost     = calc_anthropic_cost(ANTHROPIC_MODEL, input_t, output_t)
        log_cost(
            agent=self.module_name,
            provider="anthropic",
            model=ANTHROPIC_MODEL,
            tokens_used=input_t + output_t,
            cost_usd=cost,
        )
        print(f"[{self.module_name}]   Claude: {input_t + output_t} tokens - ${cost:.6f}")

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$",         "", raw.rstrip())

        proposals: list[dict] = []

        # Try JSON array first (scout_agent format)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                proposals = parsed
            elif isinstance(parsed, dict):
                proposals = [parsed]
        except json.JSONDecodeError:
            pass

        # Fall back to one-JSON-per-line (fiverr_scout format)
        if not proposals:
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        proposals.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        # Validate required fields
        required = {"opportunity_name", "platform", "how_it_works"}
        valid = [
            p for p in proposals
            if isinstance(p, dict) and required.issubset(p.keys())
        ]
        return valid[:self.max_proposals]

    def _save_proposals(self, proposals: list[dict]) -> list[dict]:
        """
        Write each proposal to scout_proposals and return the saved list.

        Normalises field names from the Claude response to the supabase_client
        ``save_proposal`` signature.  Both monthly_potential and
        monthly_potential_usd are accepted, as are missing optional fields
        (defaulting to sensible values so no proposal is silently dropped).
        """
        saved: list[dict] = []
        for prop in proposals:
            try:
                row = save_proposal(
                    opportunity_name       = prop.get("opportunity_name", "Unknown"),
                    platform               = prop.get("platform", self.platform),
                    how_it_works           = prop.get("how_it_works", ""),
                    agent_needed           = prop.get("agent_needed", "existing_with_config"),
                    setup_time_hours       = float(prop.get("setup_time_hours") or 0),
                    monthly_potential_usd  = float(
                        prop.get("monthly_potential_usd")
                        or prop.get("monthly_potential")   # fiverr_scout uses this key
                        or 0
                    ),
                    risk_description       = (
                        prop.get("risk_description")
                        or prop.get("risk_level", "")      # fiverr_scout uses this key
                        or ""
                    ),
                    credential_required    = bool(prop.get("credential_required", False)),
                    credential_instructions = prop.get("credential_instructions") or None,
                )
                saved.append(row)
                print(f"[{self.module_name}]   Saved: {prop.get('opportunity_name', '?')}")
            except Exception as e:
                print(f"[{self.module_name}]   Save failed for '{prop.get('opportunity_name')}': {e}")
        return saved
