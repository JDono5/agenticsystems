import os
import sys

# Allow running as `python core/cleanup.py` from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from core.supabase_client import supabase  # noqa: E402

AGENT_NAME = "cleanup"

# Rejected disk files are kept for this long (QA feedback loop uses them intra-day)
REJECTED_DISK_TTL = timedelta(hours=24)

# Rejected Supabase records are kept for this long (reporting history)
REJECTED_DB_TTL = timedelta(days=7)

# Absolute project root — works regardless of cwd
ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime:
    """Parse a Supabase ISO timestamp to an aware UTC datetime."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def _delete_disk_files(file_path: str) -> int:
    """
    Delete the PNG and sidecar JSON for a relative file_path.
    Returns bytes freed (0 if nothing existed on disk).
    """
    if not file_path:
        return 0
    png  = ROOT / file_path.replace("\\", "/")
    meta = png.with_suffix(".json")
    freed = 0
    for f in (png, meta):
        if f.exists():
            try:
                freed += f.stat().st_size
                f.unlink()
            except OSError as e:
                print(f"[{AGENT_NAME}]   Warning: could not delete {f}: {e}")
    return freed


def _prune_empty_dirs(base: Path) -> None:
    """Recursively remove empty subdirectories (deepest first)."""
    if not base.exists():
        return
    for d in sorted(base.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass  # not empty, skip


# ─── Orphan cleanup ───────────────────────────────────────────────────────────

def clean_orphaned_files() -> dict:
    """
    Delete PNG + JSON sidecar pairs that exist on disk but have no matching row
    in the Supabase designs table.

    Fiverr thumbnails (designs/fiverr/...) are intentionally never saved to the
    designs table (they use sidecar-only storage), so that entire subtree is
    skipped to avoid wiping delivered orders.

    Returns a summary dict.
    """
    designs_dir = ROOT / "designs"
    fiverr_dir  = ROOT / "designs" / "fiverr"

    if not designs_dir.exists():
        print(f"[{AGENT_NAME}] clean_orphaned_files: designs/ folder not found, skipping.")
        return {"orphans_deleted": 0, "bytes_freed": 0}

    # ── Build set of all known file_paths from Supabase ──────────────────────
    try:
        rows = supabase.table("designs").select("file_path").execute().data or []
    except Exception as e:
        print(f"[{AGENT_NAME}] clean_orphaned_files: DB query failed: {e}")
        return {"orphans_deleted": 0, "bytes_freed": 0}

    # Normalise to forward slashes for consistent comparison on Windows/Linux
    known_paths: set[str] = {
        r["file_path"].replace("\\", "/")
        for r in rows
        if r.get("file_path")
    }
    print(f"[{AGENT_NAME}] clean_orphaned_files: {len(known_paths)} known path(s) in DB")

    # ── Scan disk ─────────────────────────────────────────────────────────────
    orphans_deleted = 0
    bytes_freed     = 0

    for png in sorted(designs_dir.rglob("*.png")):
        # Skip Fiverr thumbnails — they intentionally have no DB row
        try:
            png.relative_to(fiverr_dir)
            continue  # path is inside designs/fiverr/ — skip
        except ValueError:
            pass  # not under fiverr_dir — proceed

        rel_fwd = str(png.relative_to(ROOT)).replace("\\", "/")
        if rel_fwd in known_paths:
            continue  # has a DB row — keep it

        # Orphan: delete PNG and JSON sidecar
        meta  = png.with_suffix(".json")
        freed = 0
        for f in (png, meta):
            if f.exists():
                try:
                    freed += f.stat().st_size
                    f.unlink()
                    print(f"[{AGENT_NAME}]   Orphan deleted: {rel_fwd}")
                except OSError as e:
                    print(f"[{AGENT_NAME}]   Warning: could not delete {f}: {e}")

        if freed:
            orphans_deleted += 1
            bytes_freed     += freed

    _prune_empty_dirs(designs_dir)

    mb = bytes_freed / (1024 * 1024)
    print(
        f"[{AGENT_NAME}] clean_orphaned_files: {orphans_deleted} orphan(s) removed, "
        f"{mb:.2f} MB freed"
    )
    return {"orphans_deleted": orphans_deleted, "bytes_freed": bytes_freed}


# ─── Main cleanup logic ───────────────────────────────────────────────────────

def run_cleanup() -> dict:
    """
    Clean up stale rejected designs and published design files.

    Rules:
    - Rejected + age > 24 h  → delete PNG + JSON from disk
    - Rejected + age > 7 d   → also delete the Supabase record
    - Published              → delete PNG + JSON from disk; keep Supabase record
    - Approved (unpublished) → never touch
    - Generated              → never touch

    Returns a summary dict.
    """
    now = datetime.now(timezone.utc)

    rejected_disk_deleted = 0
    published_disk_deleted = 0
    db_records_deleted = 0
    bytes_freed = 0

    print(
        f"[{AGENT_NAME}] --- Starting "
        f"{now.strftime('%Y-%m-%d %H:%M UTC')} ---"
    )

    # ── Orphan check first ────────────────────────────────────────────────────
    orphan_result = clean_orphaned_files()
    bytes_freed  += orphan_result["bytes_freed"]

    # ── Rejected designs ─────────────────────────────────────────────────────
    rejected = (
        supabase.table("designs")
        .select("id, file_path, created_at")
        .eq("status", "rejected")
        .execute()
        .data
    )
    print(f"[{AGENT_NAME}] Found {len(rejected)} rejected design(s) to evaluate...")

    for row in rejected:
        design_id = row["id"]
        file_path = row.get("file_path", "")
        age       = now - _parse_ts(row["created_at"])

        # Delete disk files if older than 24 h
        if age > REJECTED_DISK_TTL:
            freed = _delete_disk_files(file_path)
            if freed:
                rejected_disk_deleted += 1
                bytes_freed += freed
                print(
                    f"[{AGENT_NAME}]   Disk deleted (rejected >24 h): "
                    f"{file_path} ({freed / 1024:.1f} KB)"
                )

        # Delete Supabase record if older than 7 d
        if age > REJECTED_DB_TTL:
            try:
                supabase.table("designs").delete().eq("id", design_id).execute()
                db_records_deleted += 1
                print(f"[{AGENT_NAME}]   DB record deleted (rejected >7 d): {design_id[:8]}...")
            except Exception as e:
                print(f"[{AGENT_NAME}]   DB delete failed for {design_id[:8]}...: {e}")

    # ── Published designs ─────────────────────────────────────────────────────
    published = (
        supabase.table("designs")
        .select("id, file_path")
        .eq("status", "published")
        .execute()
        .data
    )
    print(f"[{AGENT_NAME}] Found {len(published)} published design(s) to clean from disk...")

    for row in published:
        file_path = row.get("file_path", "")
        freed = _delete_disk_files(file_path)
        if freed:
            published_disk_deleted += 1
            bytes_freed += freed
            print(
                f"[{AGENT_NAME}]   Disk cleaned (published): "
                f"{file_path} ({freed / 1024:.1f} KB)"
            )

    # ── Remove empty directories ──────────────────────────────────────────────
    _prune_empty_dirs(ROOT / "designs")

    # ── Summary ───────────────────────────────────────────────────────────────
    mb_freed      = bytes_freed / (1024 * 1024)
    orphans_found = orphan_result["orphans_deleted"]
    nothing       = (rejected_disk_deleted + published_disk_deleted + db_records_deleted + orphans_found) == 0

    print(f"\n[{AGENT_NAME}] --- Cleanup complete ---")
    if nothing:
        print(f"[{AGENT_NAME}]   Nothing to clean up.")
    else:
        print(f"[{AGENT_NAME}]   Orphaned files removed  : {orphans_found}")
        print(f"[{AGENT_NAME}]   Rejected files deleted  : {rejected_disk_deleted}")
        print(f"[{AGENT_NAME}]   Published files cleaned : {published_disk_deleted}")
        print(f"[{AGENT_NAME}]   DB records deleted      : {db_records_deleted}")
        print(f"[{AGENT_NAME}]   Space freed             : {mb_freed:.2f} MB")

    return {
        "orphans_deleted":       orphans_found,
        "rejected_disk_deleted": rejected_disk_deleted,
        "published_disk_deleted": published_disk_deleted,
        "db_records_deleted":    db_records_deleted,
        "bytes_freed":           bytes_freed,
    }


if __name__ == "__main__":
    import sys as _sys
    if "--orphans-only" in _sys.argv:
        clean_orphaned_files()
    else:
        run_cleanup()
