import time
import traceback
from datetime import datetime, timezone
from core.emailer import send_alert


def api_call_with_retry(fn, max_retries: int = 3, agent_name: str = "unknown"):
    """
    Wrap any API call with exponential backoff retry.
    On final failure: logs the error, sends an email alert, and returns None.
    Failures never crash the pipeline — they skip the task and continue.
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            wait = 2 ** attempt  # 1s, 2s, 4s
            if attempt < max_retries - 1:
                print(
                    f"[{agent_name}] Attempt {attempt + 1} failed: {e}. "
                    f"Retrying in {wait}s..."
                )
                time.sleep(wait)
            else:
                _handle_final_failure(agent_name, e)
                return None


def _handle_final_failure(agent_name: str, exc: Exception) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    error_detail = traceback.format_exc()

    print(f"[{agent_name}] FAILED after all retries at {timestamp}:\n{error_detail}")

    subject = f"Agent Alert: {agent_name} failed"
    body = (
        f"Agent:     {agent_name}\n"
        f"Time:      {timestamp}\n"
        f"Error:     {exc}\n"
        f"\n"
        f"Full traceback:\n"
        f"{error_detail}"
    )

    send_alert(subject=subject, body=body)
