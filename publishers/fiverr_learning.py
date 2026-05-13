"""
publishers/fiverr_learning.py — Memory read/write for Fiverr fulfillment.

Inherits the shared _append_pattern / _get_patterns / log_success / log_failure
implementations from BaseLearning (core/pipeline_base.py).

FiverrLearning adds the Fiverr-specific public interface:
  - log_order_to_memory   (richer than the generic log_success)
  - log_review_to_memory  (tracks star ratings and review text)
  - get_niche_memory      (full niche dict for prompt building)
  - get_high/underperforming_patterns

All existing callers (fiverr_fulfillment, memory_agent) use the module-level
functions below — these are thin wrappers and their signatures are unchanged.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.memory_client as memory_client
from core.pipeline_base import BaseLearning

MODULE_NAME = "fiverr_learning"

# Max rolling entries to keep per niche / pattern list
MAX_PATTERNS = 10


# ─── Concrete learning class ──────────────────────────────────────────────────

class FiverrLearning(BaseLearning):
    """
    Fiverr-specific learning layer.

    Inherits from BaseLearning:
      - _append_pattern(key, prompt, review, rating) — rolling append + memory write
      - _get_patterns(key) -> list[str]               — read prompt strings from memory
      - log_success / log_failure                     — generic interface
      - get_best_patterns / get_worst_patterns        — generic reads

    Adds Fiverr-specific methods:
      - log_order_to_memory  — stores full order + style context per niche
      - log_review_to_memory — stores star rating, routes high/low to pattern lists
      - get_niche_memory     — reads the full niche style-notes dict

    Memory categories used (must be in the memory table check constraint):
      platform_health   — order/niche style notes + individual review records
      prompt_performance — high/low performing prompt pattern lists
    """

    platform                = "fiverr"
    memory_category_orders  = "platform_health"
    memory_category_patterns = "prompt_performance"
    max_stored              = MAX_PATTERNS

    def log_order_to_memory(
        self,
        order:         dict,
        prompt_used:   str,
        style_context: dict,
    ) -> None:
        """
        Write a fulfilled order record to memory for this niche.

        Memory key: fiverr_{niche}_style_notes
        Future orders in the same niche read this to reference what worked.
        """
        niche    = (order.get("channel_niche") or "default").lower().replace(" ", "_")
        key      = f"fiverr_{niche}_style_notes"
        order_id = order.get("order_id", "unknown")

        existing = memory_client.recall(key)
        orders   = []
        if existing:
            orders = existing.get("value", {}).get("orders", [])

        entry = {
            "order_id":      order_id,
            "video_title":   order.get("video_title", ""),
            "package_tier":  order.get("package_tier", "basic"),
            "style_context": {k: v for k, v in style_context.items() if k != "analysis_cost"},
            "prompt_used":   prompt_used[:500],
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        }
        orders = orders[-(MAX_PATTERNS - 1):] + [entry]

        try:
            memory_client.remember(
                category=self.memory_category_orders,
                key=key,
                value={"niche": niche, "orders": orders},
                confidence=0.7,
                sample_size=len(orders),
            )
            print(f"[{MODULE_NAME}]   Logged order {order_id} to memory (niche: {niche})")
        except Exception as e:
            print(f"[{MODULE_NAME}]   Memory write failed: {e}")

    def log_review_to_memory(
        self,
        order_id:    str,
        rating:      int,
        review_text: str,
        niche:       str,
        prompt_used: str,
    ) -> None:
        """
        Write a Fiverr review to memory.

        - Every review is stored individually under platform_health
        - 5-star reviews mark the prompt as high-performing
        - <4-star reviews mark the prompt as underperforming
        """
        niche_key = (niche or "default").lower().replace(" ", "_")

        # Always store the individual review
        try:
            memory_client.remember(
                category=self.memory_category_orders,
                key=f"fiverr_review_{order_id}",
                value={
                    "order_id":  order_id,
                    "rating":    rating,
                    "review":    review_text,
                    "niche":     niche_key,
                    "prompt":    prompt_used[:400],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                confidence=float(rating) / 5.0,
                sample_size=1,
            )
        except Exception as e:
            print(f"[{MODULE_NAME}]   Review memory write failed: {e}")

        # Route to performance pattern list
        if rating >= 5:
            self._append_pattern(
                f"fiverr_{niche_key}_high_performing",
                prompt_used, review_text, rating,
            )
            print(f"[{MODULE_NAME}]   5-star review for {order_id} logged as high-performing")
        elif rating < 4:
            self._append_pattern(
                f"fiverr_{niche_key}_underperforming",
                prompt_used, review_text, rating,
            )
            print(f"[{MODULE_NAME}]   {rating}-star review for {order_id} logged as underperforming")

    def get_niche_memory(self, niche: str) -> dict:
        """Return the full memory dict for a niche (past orders, style notes)."""
        niche_key = niche.lower().replace(" ", "_")
        row = memory_client.recall(f"fiverr_{niche_key}_style_notes")
        if not row:
            return {}
        return row.get("value", {})

    def get_high_performing_patterns(self, niche: str) -> list[str]:
        """Return prompts that received 5-star reviews in this niche."""
        return self._get_patterns(f"fiverr_{niche.lower()}_high_performing")

    def get_underperforming_patterns(self, niche: str) -> list[str]:
        """Return prompts that received <4-star reviews in this niche."""
        return self._get_patterns(f"fiverr_{niche.lower()}_underperforming")


# ─── Module-level singleton + public API (unchanged signatures) ───────────────

_learning = FiverrLearning()


def log_order_to_memory(
    order: dict, prompt_used: str, style_context: dict
) -> None:
    _learning.log_order_to_memory(order, prompt_used, style_context)


def log_review_to_memory(
    order_id: str, rating: int, review_text: str, niche: str, prompt_used: str
) -> None:
    _learning.log_review_to_memory(order_id, rating, review_text, niche, prompt_used)


def get_niche_memory(niche: str) -> dict:
    return _learning.get_niche_memory(niche)


def get_high_performing_patterns(niche: str) -> list[str]:
    return _learning.get_high_performing_patterns(niche)


def get_underperforming_patterns(niche: str) -> list[str]:
    return _learning.get_underperforming_patterns(niche)


# ─── Test ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    test_order = {
        "order_id":      "TEST_001",
        "channel_niche": "finance",
        "video_title":   "How I Made $500 in a Weekend",
        "package_tier":  "standard",
    }
    test_prompt = "YouTube thumbnail for finance channel. Bold text: HOW I MADE $500..."
    test_ctx    = {"dominant_colors": "green/black", "energy_level": "intense"}

    print("[learning] Writing order to memory...")
    log_order_to_memory(test_order, test_prompt, test_ctx)

    print("[learning] Writing 5-star review to memory...")
    log_review_to_memory("TEST_001", 5, "Amazing work, exactly what I wanted!", "finance", test_prompt)

    print("[learning] Reading niche memory...")
    print(json.dumps(get_niche_memory("finance"), indent=2, default=str))

    print("[learning] High-performing patterns:", len(get_high_performing_patterns("finance")))
    print("[learning] Underperforming patterns:", len(get_underperforming_patterns("finance")))
