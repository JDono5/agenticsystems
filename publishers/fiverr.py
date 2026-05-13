"""
publishers/fiverr.py — Thin entry point for the Fiverr fulfillment system.

This is the ONLY file the scheduler, publisher_agent, and any external caller
should import from. All logic lives in the sub-modules:

  fiverr_parser.py        — Email parsing, order extraction
  fiverr_analyzer.py      — GPT-4o vision analysis of buyer images
  fiverr_prompt_builder.py — Thumbnail prompt construction + niche style guides
  fiverr_fulfillment.py   — Generate -> QA -> save -> email -> log pipeline
  fiverr_learning.py      — Memory read/write after orders and reviews

Usage from scheduler/main.py:
  from publishers.fiverr import check_fiverr_orders, fulfill_order, check_for_reviews
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from publishers.fiverr_fulfillment import (
    check_fiverr_orders,
    fulfill_order,
    check_for_reviews,
    MOCK_ORDER,
)

__all__ = ["check_fiverr_orders", "fulfill_order", "check_for_reviews"]


# ─── Entry point (--test flag) ────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fiverr fulfillment pipeline")
    parser.add_argument(
        "--test", action="store_true",
        help="Run a full fulfillment cycle with a mock order (generates real images)",
    )
    parser.add_argument(
        "--check-orders", action="store_true",
        help="Check IMAP for new Fiverr orders and fulfill them",
    )
    parser.add_argument(
        "--check-reviews", action="store_true",
        help="Check IMAP for Fiverr review notifications and log to memory",
    )
    args = parser.parse_args()

    if args.test:
        print("[fiverr] Running mock order test...")
        success = fulfill_order(MOCK_ORDER)
        print(f"[fiverr] Test {'PASSED' if success else 'FAILED'}")

    elif args.check_reviews:
        reviews = check_for_reviews()
        print(f"[fiverr] Processed {len(reviews)} review(s)")

    else:
        # Default: check for and fulfill real orders
        orders = check_fiverr_orders()
        if not orders:
            print("[fiverr] No new orders found.")
        else:
            print(f"[fiverr] Processing {len(orders)} order(s)...")
            for order in orders:
                fulfill_order(order)
