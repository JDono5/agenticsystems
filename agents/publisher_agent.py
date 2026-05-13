"""
agents/publisher_agent.py — STUB (spec Section 6.3)

Full implementation requires Etsy OAuth + Printify credentials.
Build on Day 2 once the Etsy seller account is confirmed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(platform: str = "etsy") -> dict:
    """
    Orchestrate the publish flow for approved designs.
    STUB — returns empty result until Etsy + Printify credentials are available.
    """
    print(f"[publisher_agent] Stub — skipping (Etsy/Printify credentials not yet configured)")
    return {"published": 0, "failed": 0, "cost": 0.0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--platform", default="etsy")
    args = parser.parse_args()
    if args.dry_run:
        print("[publisher_agent] Dry-run mode — would publish approved designs here")
    else:
        run(platform=args.platform)
