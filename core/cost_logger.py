from datetime import datetime, timezone
from core.supabase_client import supabase


def log_cost(
    agent: str,
    provider: str,
    model: str,
    tokens_used: int,
    cost_usd: float,
) -> None:
    """
    Log a single API call to the cost_log table immediately after it completes.
    Call this after every Anthropic or OpenAI API call.

    Args:
        agent:       Name of the calling agent, e.g. 'research_agent'
        provider:    'anthropic' or 'openai'
        model:       Exact model string, e.g. 'claude-sonnet-4-5' or 'gpt-4o'
        tokens_used: Total tokens consumed (input + output)
        cost_usd:    Cost of this call in USD
    """
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "provider": provider,
        "model": model,
        "tokens_used": tokens_used,
        "cost_usd": round(cost_usd, 6),
    }

    try:
        supabase.table("cost_log").insert(row).execute()
        print(
            f"[cost_logger] {agent} / {model}: "
            f"{tokens_used} tokens — ${cost_usd:.6f}"
        )
    except Exception as e:
        # Never let a logging failure crash an agent
        print(f"[cost_logger] WARNING: failed to log cost: {e}")


# ─── Token cost helpers ───────────────────────────────────────────────────────
# Reference prices as of May 2026. Update if pricing changes.

COST_PER_1K = {
    "claude-sonnet-4-5":      {"input": 0.003,   "output": 0.015},
    "claude-3-5-haiku-latest": {"input": 0.0008,  "output": 0.004},
    "gpt-4o":                 {"input": 0.005,   "output": 0.015},
    "gpt-4o-mini":            {"input": 0.00015, "output": 0.0006},
}


def calc_anthropic_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost for an Anthropic API call from token counts."""
    rates = COST_PER_1K.get(model, {"input": 0.003, "output": 0.015})
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1000


def calc_openai_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost for an OpenAI API call from token counts."""
    rates = COST_PER_1K.get(model, {"input": 0.005, "output": 0.015})
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1000
