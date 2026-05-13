import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

_url = os.getenv("SUPABASE_URL", "")
_key = os.getenv("SUPABASE_KEY", "")

if not _url or not _key:
    raise EnvironmentError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(_url, _key)


# ─── research_briefs ──────────────────────────────────────────────────────────

def save_brief(brief: dict) -> dict:
    """
    Insert a new research brief. Returns the inserted row.
    Expected keys: niche, sub_niche, top_keywords, opportunity_summary,
                   top_competitor_titles, avg_price_point
    """
    result = supabase.table("research_briefs").insert(brief).execute()
    return result.data[0]


def get_latest_brief() -> dict | None:
    """Return the most recent research brief, or None if the table is empty."""
    result = (
        supabase.table("research_briefs")
        .select("*")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_brief_by_id(brief_id: str) -> dict | None:
    result = (
        supabase.table("research_briefs")
        .select("*")
        .eq("id", brief_id)
        .single()
        .execute()
    )
    return result.data


# ─── designs ──────────────────────────────────────────────────────────────────

def save_design(design: dict) -> dict:
    """
    Insert a new design record. Returns the inserted row.
    Expected keys: brief_id, file_path, prompt_used, generation_cost
    Status defaults to 'generated' via DB default.
    """
    result = supabase.table("designs").insert(design).execute()
    return result.data[0]


def get_designs_by_status(status: str) -> list[dict]:
    """Return all designs with the given status (generated / approved / rejected / published)."""
    result = (
        supabase.table("designs")
        .select("*")
        .eq("status", status)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


def update_design_status(design_id: str, status: str, reason: str = "") -> None:
    """Update the status (and optionally qa_reason) of a single design."""
    payload: dict = {"status": status}
    if reason:
        payload["qa_reason"] = reason
    supabase.table("designs").update(payload).eq("id", design_id).execute()


# ─── listings ─────────────────────────────────────────────────────────────────

def save_listing(listing: dict) -> dict:
    """
    Insert a new listing record. Returns the inserted row.
    Expected keys: design_id, etsy_listing_id, printify_product_id,
                   title, tags, status
    """
    result = supabase.table("listings").insert(listing).execute()
    return result.data[0]


def update_listing_status(listing_id: str, status: str) -> None:
    supabase.table("listings").update({"status": status}).eq("id", listing_id).execute()


def get_listings_by_status(status: str) -> list[dict]:
    result = (
        supabase.table("listings")
        .select("*")
        .eq("status", status)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


# ─── cost_log ─────────────────────────────────────────────────────────────────

def get_month_spend(year: int, month: int) -> float:
    """Return total USD spent on API calls in the given calendar month."""
    from datetime import datetime
    start = datetime(year, month, 1).isoformat()
    if month == 12:
        end = datetime(year + 1, 1, 1).isoformat()
    else:
        end = datetime(year, month + 1, 1).isoformat()

    result = (
        supabase.table("cost_log")
        .select("cost_usd")
        .gte("timestamp", start)
        .lt("timestamp", end)
        .execute()
    )
    return sum(float(row["cost_usd"]) for row in result.data)


# ─── sales ────────────────────────────────────────────────────────────────────

def save_sale(sale: dict) -> dict:
    """
    Upsert a sale record (idempotent on etsy_order_id).
    Expected keys: order_date, etsy_order_id, listing_id,
                   gross_revenue, printify_cost, etsy_fee
    net_profit is computed by the DB.
    """
    result = (
        supabase.table("sales")
        .upsert(sale, on_conflict="etsy_order_id")
        .execute()
    )
    return result.data[0]


def get_sales_in_range(start_iso: str, end_iso: str, platform: str = None) -> list[dict]:
    """Return all sales between two ISO timestamps, optionally filtered by platform."""
    q = (
        supabase.table("sales")
        .select("*")
        .gte("order_date", start_iso)
        .lt("order_date", end_iso)
    )
    if platform:
        q = q.eq("platform", platform)
    return q.execute().data


# ─── expenses ──────────────────────────────────────────────────────────────────

def save_expense(
    expense_date: str,
    category: str,
    platform: str,
    description: str,
    amount_usd: float,
    recurring: bool = False,
    recurring_interval: str = None,
) -> dict:
    """Insert a one-time or recurring expense record."""
    payload: dict = {
        "expense_date": expense_date,
        "category":     category,
        "platform":     platform,
        "description":  description,
        "amount_usd":   amount_usd,
        "recurring":    recurring,
    }
    if recurring_interval:
        payload["recurring_interval"] = recurring_interval
    result = supabase.table("expenses").insert(payload).execute()
    return result.data[0]


def get_expenses_in_range(start_iso: str, end_iso: str) -> list[dict]:
    result = (
        supabase.table("expenses")
        .select("*")
        .gte("expense_date", start_iso[:10])
        .lte("expense_date", end_iso[:10])
        .execute()
    )
    return result.data


# ─── memory ───────────────────────────────────────────────────────────────────

def upsert_memory(
    category: str,
    key: str,
    value: dict,
    confidence: float = 0.5,
    sample_size: int = 1,
) -> dict:
    """Insert or update a memory record keyed on (key). last_updated is refreshed."""
    from datetime import datetime, timezone
    payload = {
        "category":     category,
        "key":          key,
        "value":        value,
        "confidence":   confidence,
        "sample_size":  sample_size,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    result = (
        supabase.table("memory")
        .upsert(payload, on_conflict="key")
        .execute()
    )
    return result.data[0]


def get_memory(key: str) -> dict | None:
    result = (
        supabase.table("memory")
        .select("*")
        .eq("key", key)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_memory_by_category(category: str) -> list[dict]:
    result = (
        supabase.table("memory")
        .select("*")
        .eq("category", category)
        .execute()
    )
    return result.data


# ─── job_queue ────────────────────────────────────────────────────────────────

def enqueue_job(job_type: str, platform: str = None, payload: dict = None) -> dict:
    row: dict = {"job_type": job_type, "payload": payload or {}}
    if platform:
        row["platform"] = platform
    result = supabase.table("job_queue").insert(row).execute()
    return result.data[0]


def dequeue_job(job_type: str) -> dict | None:
    """
    Atomically claim the oldest pending job of the given type.
    Sets status to 'processing' and records picked_up_at.
    Returns the job row or None if the queue is empty.
    """
    from datetime import datetime, timezone
    # Fetch oldest pending
    result = (
        supabase.table("job_queue")
        .select("*")
        .eq("job_type", job_type)
        .eq("status", "pending")
        .order("created_at")
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    job = result.data[0]
    # Mark as processing
    supabase.table("job_queue").update({
        "status":      "processing",
        "picked_up_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job["id"]).execute()
    return job


def complete_job(job_id: str) -> dict:
    from datetime import datetime, timezone
    result = supabase.table("job_queue").update({
        "status":       "done",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()
    return result.data[0]


def fail_job(job_id: str, error_message: str) -> dict:
    from datetime import datetime, timezone
    result = supabase.table("job_queue").update({
        "status":        "failed",
        "completed_at":  datetime.now(timezone.utc).isoformat(),
        "error_message": error_message,
    }).eq("id", job_id).execute()
    return result.data[0]


# ─── scout_proposals ──────────────────────────────────────────────────────────

def save_proposal(
    opportunity_name: str,
    platform: str,
    how_it_works: str,
    agent_needed: str,
    setup_time_hours: float,
    monthly_potential_usd: float,
    risk_description: str,
    credential_required: bool = False,
    credential_instructions: str = None,
) -> dict:
    payload: dict = {
        "opportunity_name":       opportunity_name,
        "platform":               platform,
        "how_it_works":           how_it_works,
        "agent_needed":           agent_needed,
        "setup_time_hours":       setup_time_hours,
        "monthly_potential_usd":  monthly_potential_usd,
        "risk_description":       risk_description,
        "credential_required":    credential_required,
    }
    if credential_instructions:
        payload["credential_instructions"] = credential_instructions
    result = supabase.table("scout_proposals").insert(payload).execute()
    return result.data[0]


def get_pending_proposals() -> list[dict]:
    result = (
        supabase.table("scout_proposals")
        .select("*")
        .eq("status", "pending")
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


def approve_proposal(proposal_id: str) -> dict:
    from datetime import datetime, timezone
    result = supabase.table("scout_proposals").update({
        "status":      "approved",
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", proposal_id).execute()
    return result.data[0]


def ignore_proposal(proposal_id: str) -> dict:
    result = supabase.table("scout_proposals").update({"status": "ignored"}).eq("id", proposal_id).execute()
    return result.data[0]
