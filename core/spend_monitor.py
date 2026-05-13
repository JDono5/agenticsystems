import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from core.supabase_client import get_month_spend
from core.emailer import send_alert

load_dotenv()

MONTHLY_CAP = float(os.getenv("MONTHLY_SPEND_CAP", "100"))


def check_cap() -> bool:
    """
    Check whether the monthly API spend is still under the cap.
    Call this at the top of every agent run before making any API calls.

    Returns:
        True  — spend is under cap, safe to proceed
        False — cap is hit or exceeded, all agents must halt
    """
    now = datetime.now(timezone.utc)
    total_spent = get_month_spend(now.year, now.month)

    if total_spent >= MONTHLY_CAP:
        _alert_cap_hit(total_spent, now)
        return False

    remaining = MONTHLY_CAP - total_spent
    print(
        f"[spend_monitor] ${total_spent:.2f} spent this month "
        f"(cap: ${MONTHLY_CAP:.2f}, remaining: ${remaining:.2f})"
    )
    return True


def get_current_spend() -> float:
    """Return the raw spend total for the current calendar month."""
    now = datetime.now(timezone.utc)
    return get_month_spend(now.year, now.month)


def _alert_cap_hit(total_spent: float, now: datetime) -> None:
    month_label = now.strftime("%B %Y")
    print(
        f"[spend_monitor] HALT — monthly cap of ${MONTHLY_CAP:.2f} reached. "
        f"Total spent: ${total_spent:.2f}. All agents paused."
    )
    send_alert(
        subject=f"SPEND CAP HIT — Agents Paused ({month_label})",
        body=(
            f"Monthly spend cap reached. All agents are now halted.\n\n"
            f"Month:     {month_label}\n"
            f"Cap:       ${MONTHLY_CAP:.2f}\n"
            f"Spent:     ${total_spent:.2f}\n"
            f"Over by:   ${total_spent - MONTHLY_CAP:.2f}\n\n"
            f"Agents will resume automatically on the 1st of next month.\n"
            f"To raise the cap, update MONTHLY_SPEND_CAP in your Railway environment variables."
        ),
    )
