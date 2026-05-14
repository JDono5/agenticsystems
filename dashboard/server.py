import os
import re
import sys
import json
import time
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT))
from core.supabase_client import supabase, update_design_status  # noqa: E402

app = Flask(__name__)
_agent_lock  = threading.Lock()
_status_lock = threading.Lock()

print(f"[server] v2.0 — all endpoints loaded | file: {__file__}")

# ─── Seed activity_log on startup ─────────────────────────────────────────────
def _seed_activity_log():
    """Insert one system event so the activity panel is never empty on first load."""
    try:
        from core.activity_logger import log_activity
        log_activity("server", "system", "Dashboard server started")
    except Exception as e:
        print(f"[server] activity_log seed skipped: {e}")

# Run in background thread so a missing table never delays startup
threading.Thread(target=_seed_activity_log, daemon=True).start()

# ─── Agent status tracker ─────────────────────────────────────────────────────
# Each key is the agent_name string passed to _stream_agent.
agent_status: dict = {}

# Monthly spend cache — refreshed at most every 30 s to keep /agent-status cheap
_spend_cache: dict = {"value": 0.0, "by_provider": {}, "at": 0.0}


def _get_cached_spend() -> float:
    """Return total monthly spend; refresh cache if stale."""
    _refresh_spend_cache()
    return _spend_cache["value"]


def _get_spend_breakdown() -> dict:
    """Return {total, by_provider: {anthropic, openai, google, ...}} for the current month."""
    _refresh_spend_cache()
    return {
        "total":       _spend_cache["value"],
        "by_provider": dict(_spend_cache["by_provider"]),
    }


def _refresh_spend_cache() -> None:
    now = time.monotonic()
    if now - _spend_cache["at"] <= 30:
        return
    try:
        month_start = datetime(
            datetime.now(timezone.utc).year,
            datetime.now(timezone.utc).month,
            1, tzinfo=timezone.utc,
        ).isoformat()
        rows = _safe_data(
            supabase.table("cost_log")
            .select("provider, cost_usd")
            .gte("timestamp", month_start)
        )
        total = 0.0
        by_provider: dict = {}
        for r in rows:
            c = float(r.get("cost_usd") or 0)
            p = r.get("provider") or "unknown"
            total += c
            by_provider[p] = round(by_provider.get(p, 0.0) + c, 6)
        _spend_cache["value"]       = round(total, 4)
        _spend_cache["by_provider"] = by_provider
        _spend_cache["at"]          = now
    except Exception:
        pass


# Pre-warm the spend cache so the first /agent-status call returns real data
threading.Thread(target=_refresh_spend_cache, daemon=True).start()


def _set_status(name: str, **kwargs) -> None:
    with _status_lock:
        entry = agent_status.setdefault(name, {
            "name":         name,
            "status":       "idle",
            "progress":     0,
            "current_step": "",
            "started_at":   None,
            "last_update":  None,
            "completed_at": None,
            "cost_this_run": 0.0,
        })
        entry.update(kwargs)
        entry["last_update"] = datetime.now(timezone.utc).isoformat()


def _apply_progress(name: str, line: str) -> None:
    """Parse a single agent output line and update that agent's status entry."""
    low = line.lower()

    # Design agent: "Attempt 3/24" or "attempt 3/24"
    m = re.search(r'attempt\s+(\d+)/(\d+)', low)
    if m:
        cur, tot = int(m.group(1)), int(m.group(2))
        _set_status(name, progress=min(95, round(cur / tot * 100)),
                    current_step=f"Attempt {cur}/{tot}")
        return

    # Approved count: "Approved: 3/8"
    m = re.search(r'approved[:\s]+(\d+)/(\d+)', low)
    if m:
        cur, tot = int(m.group(1)), int(m.group(2))
        _set_status(name, progress=min(95, round(cur / tot * 100)),
                    current_step=f"Approved {cur}/{tot}")
        return

    # Starting line
    if re.search(r'---\s*starting', low):
        _set_status(name, progress=5, current_step="Starting…")
        return

    # Completion signals (before __DONE__ sentinel)
    if any(x in low for x in ["complete ---", "run complete", "cleanup complete",
                                "done ---", "proposals saved", "memory written"]):
        _set_status(name, progress=95, current_step="Finishing…")
        return

    # Error signals (ignore lines that merely say "no errors")
    if ("error" in low or "traceback" in low or "exception" in low) \
            and "no error" not in low and "0 error" not in low:
        _set_status(name, status="error", current_step=line[:100])
        return

    # Generic activity: any [agent] prefixed line → update current_step
    m = re.match(r'^\[[^\]]+\]\s+(.*)', line)
    if m:
        step = m.group(1).strip()
        if step and len(step) > 2:
            _set_status(name, current_step=step[:100])


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.isoformat()

def _week_start() -> str:
    now    = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return _iso(monday)

def _month_start() -> str:
    now = datetime.now(timezone.utc)
    return _iso(datetime(now.year, now.month, 1, tzinfo=timezone.utc))

def _today_start() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00+00:00")

def _safe_count(q) -> int:
    try:
        return q.execute().count or 0
    except Exception:
        return 0

def _safe_data(q) -> list:
    try:
        return q.execute().data or []
    except Exception:
        return []


# ─── Static files ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(Path(__file__).parent), "index.html")

@app.route("/designs/<path:filename>")
def serve_design_file(filename):
    return send_from_directory(str(ROOT / "designs"), filename)


# ─── Config ───────────────────────────────────────────────────────────────────

@app.route("/config")
def config():
    return jsonify({
        "supabaseUrl": os.getenv("SUPABASE_URL", ""),
        "supabaseKey": os.getenv("SUPABASE_KEY", ""),
        "spendCap":    float(os.getenv("MONTHLY_SPEND_CAP", "100")),
    })


# ─── Agent SSE runner ─────────────────────────────────────────────────────────

def _stream_agent(cmd: list[str], agent_name: str = ""):
    def generate():
        if not _agent_lock.acquire(blocking=False):
            yield f"data: {json.dumps('[dashboard] Another agent is already running.')}\n\n"
            return

        if agent_name:
            _set_status(agent_name,
                        status="running", progress=0,
                        current_step="Starting…",
                        started_at=datetime.now(timezone.utc).isoformat(),
                        completed_at=None, cost_this_run=0.0)

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"   # prevent cp1252 crash on Windows
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
            )
            for line in proc.stdout:
                stripped = line.rstrip()
                yield f"data: {json.dumps(stripped)}\n\n"
                if agent_name:
                    _apply_progress(agent_name, stripped)

            proc.wait()
            exit_code = proc.returncode
            yield f"data: {json.dumps('__DONE__:' + str(exit_code))}\n\n"

            if agent_name:
                if exit_code == 0:
                    _set_status(agent_name, status="complete", progress=100,
                                current_step="Done",
                                completed_at=datetime.now(timezone.utc).isoformat())
                else:
                    _set_status(agent_name, status="error", progress=0,
                                current_step="Failed (see log)",
                                completed_at=datetime.now(timezone.utc).isoformat())
        finally:
            _agent_lock.release()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Run endpoints ────────────────────────────────────────────────────────────

@app.route("/run/research",          methods=["POST"])
def run_research():
    platform = (request.get_json(silent=True) or {}).get("platform", "etsy")
    args = [sys.executable, "agents/research_agent.py"]
    if platform == "fiverr":
        args += ["--platform", "fiverr"]
    return _stream_agent(args, agent_name="research")

@app.route("/run/design",            methods=["POST"])
def run_design():
    return _stream_agent([sys.executable, "agents/design_agent.py"], agent_name="design")

@app.route("/run/design/full",       methods=["POST"])
def run_design_full():
    return _stream_agent([sys.executable, "agents/design_agent.py", "--full"], agent_name="design")

@app.route("/run/qa",                methods=["POST"])
def run_qa():
    return _stream_agent([sys.executable, "agents/qa_agent.py"], agent_name="qa")

@app.route("/run/cleanup",           methods=["POST"])
def run_cleanup():
    return _stream_agent([sys.executable, "core/cleanup.py"], agent_name="cleanup")

@app.route("/run/cleanup_orphans",   methods=["POST"])
def run_cleanup_orphans():
    return _stream_agent([sys.executable, "core/cleanup.py", "--orphans-only"], agent_name="cleanup")


@app.route("/run/pipeline_reset",    methods=["POST"])
def run_pipeline_reset():
    """Force-release the pipeline lock in case a previous run got stuck."""
    global _pipeline_lock_ts
    released = False
    if _pipeline_lock.locked():
        try:
            _pipeline_lock.release()
            released = True
        except RuntimeError:
            pass
    _pipeline_lock_ts = 0.0
    with _run_state_lock:
        _run_state["pipeline_running"] = False
        _run_state["current_stage"]    = None
    return jsonify({"released": released, "message": "Pipeline lock released" if released else "Lock was not held"})

@app.route("/run/scout",             methods=["POST"])
def run_scout():
    return _stream_agent([sys.executable, "agents/scout_agent.py", "--mock"], agent_name="scout")

@app.route("/run/fiverr_scout",      methods=["POST"])
def run_fiverr_scout():
    return _stream_agent([sys.executable, "publishers/fiverr_scout.py", "--mock"], agent_name="fiverr_scout")

@app.route("/run/fiverr_orders",     methods=["POST"])
def run_fiverr_orders():
    return _stream_agent([sys.executable, "publishers/fiverr.py", "--check-orders"], agent_name="fiverr_orders")

@app.route("/run/fiverr_reviews",    methods=["POST"])
def run_fiverr_reviews():
    return _stream_agent([sys.executable, "publishers/fiverr.py", "--check-reviews"], agent_name="fiverr_reviews")

@app.route("/run/fiverr_test",       methods=["POST"])
def run_fiverr_test():
    return _stream_agent([sys.executable, "publishers/fiverr.py", "--test"], agent_name="fiverr_test")

@app.route("/run/memory",            methods=["POST"])
def run_memory():
    return _stream_agent([sys.executable, "agents/memory_agent.py"], agent_name="memory")

@app.route("/run/anomaly",           methods=["POST"])
def run_anomaly():
    return _stream_agent([sys.executable, "agents/anomaly_detector.py"], agent_name="anomaly")

@app.route("/run/prompt_evolution",  methods=["POST"])
def run_prompt_evolution():
    return _stream_agent([sys.executable, "agents/prompt_evolution_agent.py", "--dry-run"], agent_name="prompt_evolution")

@app.route("/run/reporting",         methods=["POST"])
def run_reporting():
    return _stream_agent([sys.executable, "agents/reporting_agent.py"], agent_name="reporting")

@app.route("/run/performance",       methods=["POST"])
def run_performance():
    return _stream_agent([sys.executable, "agents/performance_agent.py"], agent_name="performance")

@app.route("/run/publisher",         methods=["POST"])
def run_publisher():
    return _stream_agent([sys.executable, "agents/publisher_agent.py"], agent_name="publisher")


# ─── Pipeline run state (survives client disconnects) ─────────────────────────
_ALL_STAGES = ["research", "design", "qa"]
_MAX_LOG_LINES = 500

_run_state: dict = {
    "pipeline_running": False,
    "current_stage":    None,
    "stage_logs":       {s: [] for s in _ALL_STAGES},
    "stage_results":    {},
    "completed_stages": [],
    "started_at":       None,
    "platform":         "etsy",
}
_run_state_lock     = threading.Lock()
_pipeline_lock      = threading.Lock()
_pipeline_lock_ts   = 0.0  # epoch time when lock was last acquired


def _rs_append(stage: str, line: str) -> None:
    """Append a log line to the run state (thread-safe, capped at _MAX_LOG_LINES)."""
    with _run_state_lock:
        log = _run_state["stage_logs"].setdefault(stage, [])
        log.append(line)
        if len(log) > _MAX_LOG_LINES:
            del log[0]


def _rs_reset(platform: str = "etsy") -> None:
    """Reset run state before starting a new pipeline run."""
    with _run_state_lock:
        _run_state.update({
            "pipeline_running": True,
            "current_stage":    None,
            "stage_logs":       {s: [] for s in _ALL_STAGES},
            "stage_results":    {},
            "completed_stages": [],
            "started_at":       datetime.now(timezone.utc).isoformat(),
            "platform":         platform,
        })


def _run_pipeline_thread(platform: str) -> None:
    """
    Run all 3 pipeline stages in a background thread so the pipeline
    survives client disconnects. Logs every line into _run_state so
    reconnecting clients can replay history.
    """
    stages = [
        (
            [sys.executable, "agents/research_agent.py"]
            + (["--platform", "fiverr"] if platform == "fiverr" else []),
            "research",
        ),
        ([sys.executable, "agents/design_agent.py", "--full"], "design"),
        ([sys.executable, "agents/qa_agent.py"],               "qa"),
    ]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        for args, name in stages:
            print(f"[full_pipeline] Starting {name} stage", flush=True)
            with _run_state_lock:
                _run_state["current_stage"] = name

            _set_status(name, status="running", progress=5,
                        current_step=f"{name} starting...",
                        started_at=datetime.now(timezone.utc).isoformat(),
                        completed_at=None, cost_this_run=0.0)

            proc = subprocess.Popen(
                args, cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
            )

            for raw_line in proc.stdout:
                stripped = raw_line.rstrip()
                _rs_append(name, stripped)
                _apply_progress(name, stripped)

            proc.wait()
            ok = proc.returncode == 0
            print(f"[full_pipeline] {name} {'complete' if ok else 'FAILED'} (exit {proc.returncode})", flush=True)

            _set_status(name,
                        status="complete" if ok else "error",
                        progress=100 if ok else 0,
                        current_step="Done" if ok else "Failed",
                        completed_at=datetime.now(timezone.utc).isoformat())

            result_msg = (
                f"--- [{name.upper()}] complete ---" if ok
                else f"--- [{name.upper()}] FAILED (exit {proc.returncode}) ---"
            )
            _rs_append(name, result_msg)

            with _run_state_lock:
                _run_state["completed_stages"].append(name)
                _run_state["stage_results"][name] = "complete" if ok else "error"

            if not ok:
                break

        all_ok = all(v == "complete" for v in _run_state["stage_results"].values())
        if all_ok:
            _rs_append("qa", "--- [PIPELINE] all stages complete ---")

    except Exception as exc:
        print(f"[full_pipeline] Thread error: {exc}", flush=True)
        cur = _run_state.get("current_stage")
        if cur:
            _rs_append(cur, f"[pipeline] Error: {exc}")

    finally:
        with _run_state_lock:
            _run_state["pipeline_running"] = False
            _run_state["current_stage"]    = None
        try:
            _pipeline_lock.release()
        except RuntimeError:
            pass


def _pipeline_sse_stream(reconnect: bool = False):
    """
    Shared SSE response factory used by both the POST (initial run) and
    GET /run/full_pipeline/stream (reconnect) endpoints.

    reconnect=False  — waits up to 5 s for the background thread to start,
                       then tails _run_state live.
    reconnect=True   — replays all existing logs first, then tails live.
    """

    def generate():
        yield ": heartbeat\n\n"

        if reconnect and not _run_state["pipeline_running"] and not _run_state["completed_stages"]:
            yield f"data: {json.dumps('[stream] No pipeline running or recent history.')}\n\n"
            return

        seen  = {s: 0 for s in _ALL_STAGES}
        sents: set = set()   # stages whose separator has already been sent

        # ── Replay existing logs (reconnect path) ─────────────────────────────
        if reconnect:
            yield f"data: {json.dumps('--- [RECONNECTED] replaying history ---')}\n\n"
            for stage in _ALL_STAGES:
                logs = list(_run_state["stage_logs"].get(stage, []))
                if not logs:
                    continue
                yield f"data: {json.dumps(f'--- [{stage.upper()}] stage starting ---')}\n\n"
                sents.add(stage)
                for line in logs:
                    yield f"data: {json.dumps(line)}\n\n"
                seen[stage] = len(logs)

            if not _run_state["pipeline_running"]:
                all_ok = all(v == "complete" for v in _run_state["stage_results"].values())
                yield f"data: {json.dumps('--- [RECONNECTED] pipeline already finished ---')}\n\n"
                yield f"data: {json.dumps('__DONE__:' + ('0' if all_ok else '1'))}\n\n"
                return

            yield f"data: {json.dumps('--- [RECONNECTED] now live ---')}\n\n"

        # ── Wait for thread to mark pipeline_running (initial path) ───────────
        if not reconnect:
            waited = 0
            while not _run_state["pipeline_running"] and waited < 50:
                time.sleep(0.1)
                waited += 1

        # ── Live tail ─────────────────────────────────────────────────────────
        prev_stage = None
        while True:
            is_running = _run_state["pipeline_running"]
            cur        = _run_state.get("current_stage")

            # Emit stage separator on transition
            if cur and cur != prev_stage and cur not in sents:
                yield f"data: {json.dumps(f'--- [{cur.upper()}] stage starting ---')}\n\n"
                sents.add(cur)
                prev_stage = cur

            # Drain new lines for current stage
            if cur:
                logs = _run_state["stage_logs"].get(cur, [])
                pos  = seen.get(cur, 0)
                for line in logs[pos:]:
                    yield f"data: {json.dumps(line)}\n\n"
                seen[cur] = len(logs)

            if not is_running:
                # Final drain — catch any lines written after the flag flipped
                for stage in _ALL_STAGES:
                    logs = _run_state["stage_logs"].get(stage, [])
                    pos  = seen.get(stage, 0)
                    for line in logs[pos:]:
                        yield f"data: {json.dumps(line)}\n\n"
                    seen[stage] = len(logs)

                all_ok = all(v == "complete" for v in _run_state.get("stage_results", {}).values())
                yield f"data: {json.dumps('__DONE__:' + ('0' if all_ok else '1'))}\n\n"
                return

            time.sleep(0.1)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/run/full_pipeline", methods=["POST"])
def run_full_pipeline():
    """Start the pipeline (or reconnect to one already running)."""
    platform = (request.get_json(silent=True) or {}).get("platform", "etsy")
    print(f"[full_pipeline] Request received - platform={platform}", flush=True)

    global _pipeline_lock_ts

    if not _pipeline_lock.acquire(blocking=False):
        age = time.time() - _pipeline_lock_ts
        if age > 600:
            print(f"[full_pipeline] Stale lock ({age:.0f}s) - force-releasing", flush=True)
            try:
                _pipeline_lock.release()
            except RuntimeError:
                pass
            _pipeline_lock.acquire(blocking=True)
        else:
            # Already running — reconnect the caller to the live stream
            print("[full_pipeline] Already running - reconnecting client", flush=True)
            return _pipeline_sse_stream(reconnect=True)

    _pipeline_lock_ts = time.time()
    _rs_reset(platform)

    t = threading.Thread(target=_run_pipeline_thread, args=(platform,), daemon=True)
    t.start()

    return _pipeline_sse_stream(reconnect=False)


@app.route("/run/full_pipeline/stream")
def stream_pipeline_live():
    """Read-only reconnect endpoint — replays history then streams live."""
    return _pipeline_sse_stream(reconnect=True)


@app.route("/run/state")
def get_run_state():
    """Return current pipeline run state for frontend reconnection on reload."""
    with _run_state_lock:
        state = {
            "pipeline_running": _run_state["pipeline_running"],
            "current_stage":    _run_state["current_stage"],
            "started_at":       _run_state["started_at"],
            "platform":         _run_state["platform"],
            "completed_stages": list(_run_state["completed_stages"]),
            "stage_results":    dict(_run_state["stage_results"]),
            # Truncate logs to last 200 lines per stage for the initial state snapshot
            "stage_logs": {
                s: list(l[-200:])
                for s, l in _run_state["stage_logs"].items()
            },
        }
    return jsonify(state)


# ─── Scheduler process manager ────────────────────────────────────────────────
_scheduler_proc: subprocess.Popen | None = None
_scheduler_lock = threading.Lock()


@app.route("/scheduler/status")
def scheduler_status_route():
    with _scheduler_lock:
        running = _scheduler_proc is not None and _scheduler_proc.poll() is None
    return jsonify({"running": running, "pid": _scheduler_proc.pid if running else None})


@app.route("/scheduler/start", methods=["POST"])
def scheduler_start():
    global _scheduler_proc
    with _scheduler_lock:
        if _scheduler_proc is not None and _scheduler_proc.poll() is None:
            return jsonify({"ok": True, "status": "already_running", "pid": _scheduler_proc.pid})
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        _scheduler_proc = subprocess.Popen(
            [sys.executable, "scheduler/main.py"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            from core.activity_logger import log_activity
            log_activity("scheduler", "system", f"Scheduler started (pid {_scheduler_proc.pid})")
        except Exception:
            pass
    return jsonify({"ok": True, "status": "started", "pid": _scheduler_proc.pid})


@app.route("/scheduler/stop", methods=["POST"])
def scheduler_stop():
    global _scheduler_proc
    with _scheduler_lock:
        if _scheduler_proc is None or _scheduler_proc.poll() is not None:
            _scheduler_proc = None
            return jsonify({"ok": True, "status": "not_running"})
        pid = _scheduler_proc.pid
        _scheduler_proc.terminate()
        try:
            _scheduler_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _scheduler_proc.kill()
        _scheduler_proc = None
        try:
            from core.activity_logger import log_activity
            log_activity("scheduler", "system", f"Scheduler stopped (pid {pid})")
        except Exception:
            pass
    return jsonify({"ok": True, "status": "stopped"})


# ─── Agent status ─────────────────────────────────────────────────────────────

@app.route("/agent-status")
def get_agent_status():
    with _status_lock:
        snapshot = {k: dict(v) for k, v in agent_status.items()}
    # Always re-read from env so changes to .env take effect without restart
    breakdown = _get_spend_breakdown()
    snapshot["__spend__"] = {
        "month_spend": breakdown["total"],
        "spend_cap":   float(os.getenv("MONTHLY_SPEND_CAP", "100")),
        "by_provider": breakdown["by_provider"],
    }
    return jsonify(snapshot)


# ─── Core stats (overview) ────────────────────────────────────────────────────

@app.route("/stats")
def stats():
    today       = _today_start()
    month_start = _month_start()
    week        = _week_start()
    now         = _iso(datetime.now(timezone.utc))
    try:
        gen   = _safe_count(supabase.table("designs").select("id", count="exact").gte("created_at", today))
        appr  = _safe_count(supabase.table("designs").select("id", count="exact").gte("created_at", today).eq("status", "approved"))
        rej   = _safe_count(supabase.table("designs").select("id", count="exact").gte("created_at", today).eq("status", "rejected"))
        costs = _safe_data(supabase.table("cost_log").select("cost_usd").gte("timestamp", month_start))
        pubs  = _safe_count(supabase.table("listings").select("id", count="exact").eq("status", "active"))
        spend = round(sum(float(r["cost_usd"]) for r in costs), 4)

        # Weekly revenue across all platforms
        etsy_sales = _safe_data(
            supabase.table("sales").select("gross_revenue, net_profit")
            .gte("order_date", week).lt("order_date", now)
        )
        week_revenue = round(sum(float(r.get("gross_revenue") or 0) for r in etsy_sales), 2)
        week_net     = round(sum(float(r.get("net_profit") or 0)    for r in etsy_sales), 2)

        return jsonify({
            "designed_today":     gen,
            "approved_today":     appr,
            "rejected_today":     rej,
            "month_spend":        spend,
            "spend_cap":          float(os.getenv("MONTHLY_SPEND_CAP", "100")),
            "listings_published": pubs,
            "week_revenue":       week_revenue,
            "week_net":           week_net,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Etsy stats ───────────────────────────────────────────────────────────────

@app.route("/etsy/stats")
def etsy_stats():
    week = _week_start()
    now  = _iso(datetime.now(timezone.utc))
    try:
        active = _safe_count(supabase.table("listings").select("id", count="exact").eq("platform", "etsy").eq("status", "active"))
        drafts = _safe_count(supabase.table("listings").select("id", count="exact").eq("platform", "etsy").eq("status", "draft"))

        # Try platform filter; fall back to all sales if column doesn't exist yet
        try:
            sales = _safe_data(
                supabase.table("sales").select("gross_revenue, net_profit")
                .eq("platform", "etsy").gte("order_date", week).lt("order_date", now)
            )
        except Exception:
            sales = []

        revenue = round(sum(float(r.get("gross_revenue") or 0) for r in sales), 2)
        net     = round(sum(float(r.get("net_profit") or 0)    for r in sales), 2)
        avg_net = round(net / len(sales), 2) if sales else 0.0

        printify_key  = os.getenv("PRINTIFY_API_KEY",  "").strip()
        printify_shop = os.getenv("PRINTIFY_SHOP_ID", "").strip()
        if printify_key and printify_shop:
            etsy_status = "Active via Printify"
        else:
            etsy_status = "Not configured"

        return jsonify({
            "active_listings":      active,
            "draft_listings":       drafts,
            "revenue_this_week":    revenue,
            "net_this_week":        net,
            "orders_this_week":     len(sales),
            "avg_net_per_sale":     avg_net,
            "etsy_connection_status": etsy_status,
        })
    except Exception as e:
        return jsonify({
            "active_listings": 0, "draft_listings": 0,
            "revenue_this_week": 0, "net_this_week": 0,
            "orders_this_week": 0, "avg_net_per_sale": 0,
            "error": str(e),
        })


# ─── Fiverr stats ─────────────────────────────────────────────────────────────

@app.route("/fiverr/stats")
def fiverr_stats():
    week = _week_start()
    now  = _iso(datetime.now(timezone.utc))
    try:
        # Orders this week — live from sales table, no caching
        week_sales = _safe_data(
            supabase.table("sales").select("gross_revenue, net_profit")
            .eq("platform", "fiverr")
            .gte("order_date", week).lt("order_date", now)
        )
        orders_count = len(week_sales)
        revenue = round(sum(float(r.get("gross_revenue") or 0) for r in week_sales), 2)
        print(f"[fiverr/stats] week={week} sales query returned {orders_count} rows, revenue=${revenue}")

        # Delivered total (all time) — live from sales table
        all_sales = _safe_data(
            supabase.table("sales").select("id", count="exact")
            .eq("platform", "fiverr")
        )
        delivered = len(all_sales)
        print(f"[fiverr/stats] delivered_total (all-time sales rows) = {delivered}")

        # Avg rating from memory
        avg_rating = None
        try:
            mem = _safe_data(
                supabase.table("memory").select("value").eq("key", "fiverr_overall_avg_rating")
            )
            if mem:
                v = mem[0].get("value", {})
                avg_rating = v.get("avg_rating") if isinstance(v, dict) else None
        except Exception:
            pass

        # Gig type breakdown from designs table
        gig_counts = {"thumbnail": 0, "logo": 0, "social_media": 0}
        try:
            gig_rows = _safe_data(
                supabase.table("designs").select("gig_type")
                .eq("platform", "fiverr")
            )
            for row in gig_rows:
                gt = row.get("gig_type") or "thumbnail"
                if gt in gig_counts:
                    gig_counts[gt] += 1
                else:
                    gig_counts[gt] = 1
        except Exception:
            pass

        return jsonify({
            "orders_this_week":  orders_count,
            "revenue_this_week": revenue,
            "avg_rating":        avg_rating,
            "pending_orders":    0,
            "delivered_total":   delivered,
            "gig_breakdown":     gig_counts,
        })
    except Exception as e:
        print(f"[fiverr/stats] ERROR: {e}")
        return jsonify({
            "orders_this_week": 0, "revenue_this_week": 0,
            "avg_rating": None, "pending_orders": 0, "delivered_total": 0,
            "gig_breakdown": {"thumbnail": 0, "logo": 0, "social_media": 0},
            "error": str(e),
        })


# ─── Fiverr recent orders ─────────────────────────────────────────────────────

@app.route("/fiverr/orders")
def fiverr_orders():
    try:
        rows = _safe_data(
            supabase.table("sales").select("*")
            .eq("platform", "fiverr")
            .order("order_date", desc=True)
            .limit(20)
        )
        return jsonify(rows)
    except Exception:
        return jsonify([])


# ─── Design stats (platform-specific) ─────────────────────────────────────────

@app.route("/designs/etsy/stats")
def etsy_design_stats():
    today = _today_start()
    try:
        generated = _safe_count(
            supabase.table("designs").select("id", count="exact")
            .eq("platform", "etsy").gte("created_at", today)
        )
        approved = _safe_count(
            supabase.table("designs").select("id", count="exact")
            .eq("platform", "etsy").eq("status", "approved").gte("created_at", today)
        )
        rejected = _safe_count(
            supabase.table("designs").select("id", count="exact")
            .eq("platform", "etsy").eq("status", "rejected").gte("created_at", today)
        )
        published = _safe_count(
            supabase.table("designs").select("id", count="exact")
            .eq("platform", "etsy").eq("status", "published")
        )
        rate = round(approved / generated * 100) if generated else 0
        return jsonify({
            "generated_today": generated,
            "approved_today":  approved,
            "rejected_today":  rejected,
            "published_total": published,
            "approval_rate":   rate,
        })
    except Exception as e:
        return jsonify({
            "generated_today": 0, "approved_today": 0,
            "rejected_today": 0, "published_total": 0, "approval_rate": 0,
            "error": str(e),
        })


@app.route("/designs/fiverr/stats")
def fiverr_design_stats():
    week = _week_start()
    now  = _iso(datetime.now(timezone.utc))
    try:
        # Use filesystem as source of truth for Fiverr designs
        fiverr_dir = ROOT / "designs" / "fiverr"
        total_files = 0
        total_cost  = 0.0
        total_attempts = 0
        if fiverr_dir.exists():
            pngs = list(fiverr_dir.rglob("*.png"))
            total_files = len(pngs)
            for png in pngs:
                mf = png.with_suffix(".json")
                if mf.exists():
                    try:
                        m = json.loads(mf.read_text(encoding="utf-8"))
                        total_cost += float(m.get("cost", 0) or 0)
                    except Exception:
                        pass
            total_attempts = total_files  # each file = one delivered order

        # Orders fulfilled this week from cost_log
        orders_week = _safe_count(
            supabase.table("cost_log").select("id", count="exact")
            .eq("agent", "fiverr_fulfillment")
            .gte("timestamp", week).lt("timestamp", now)
        )

        return jsonify({
            "orders_this_week":         orders_week,
            "delivered_total":          total_files,
            "pending_delivery":         0,
            "avg_attempts_per_order":   round(total_attempts / total_files, 1) if total_files else 0,
            "total_generation_cost":    round(total_cost, 4),
        })
    except Exception as e:
        return jsonify({
            "orders_this_week": 0, "delivered_total": 0, "pending_delivery": 0,
            "avg_attempts_per_order": 0, "total_generation_cost": 0,
            "error": str(e),
        })


# ─── Memory ───────────────────────────────────────────────────────────────────

@app.route("/memory")
def memory():
    category = request.args.get("category", "")
    try:
        q = supabase.table("memory").select("*").order("last_updated", desc=True).limit(50)
        if category:
            q = q.eq("category", category)
        rows = _safe_data(q)
        return jsonify(rows)
    except Exception:
        return jsonify([])


# ─── Job queue ────────────────────────────────────────────────────────────────

@app.route("/jobs")
def jobs():
    try:
        rows = _safe_data(
            supabase.table("job_queue").select("*")
            .order("created_at", desc=True)
            .limit(30)
        )
        return jsonify(rows)
    except Exception:
        return jsonify([])


# ─── Finance ──────────────────────────────────────────────────────────────────

@app.route("/finance")
def finance():
    try:
        from core.finance import get_weekly_pnl, get_all_time_pnl
        now  = datetime.now(timezone.utc)
        week = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()

        # Monthly spend & per-agent breakdown
        spend_rows = _safe_data(
            supabase.table("cost_log").select("cost_usd, agent").gte("timestamp", month_start)
        )
        month_spend = round(sum(float(r["cost_usd"]) for r in spend_rows), 4)
        by_agent: dict = {}
        for r in spend_rows:
            a = r.get("agent", "unknown")
            by_agent[a] = round(by_agent.get(a, 0.0) + float(r["cost_usd"]), 4)

        # Expenses — use correct column name: expense_date
        expenses = _safe_data(
            supabase.table("expenses").select("*").order("expense_date", desc=True).limit(20)
        )

        # Always show something in finance even with zero revenue
        weekly_pnl   = {}
        all_time_pnl = {}
        try:
            weekly_pnl = get_weekly_pnl(week, now.isoformat())
        except Exception as we:
            weekly_pnl = {
                "gross_revenue": 0, "api_costs": month_spend, "fulfillment_costs": 0,
                "platform_fees": 0, "expense_costs": 0, "total_costs": month_spend,
                "net_profit": -month_spend, "net_margin_pct": 0, "order_count": 0,
                "error": str(we),
            }
        try:
            all_time_pnl = get_all_time_pnl()
        except Exception as ate:
            total_expenses = sum(float(e.get("amount_usd", 0)) for e in expenses)
            all_time_pnl = {
                "gross_revenue": 0, "api_costs": month_spend,
                "net_profit": -(total_expenses + month_spend),
                "setup_costs_recovered": False,
                "remaining_to_recover": round(total_expenses + month_spend, 2),
                "days_since_launch": 0,
                "error": str(ate),
            }

        return jsonify({
            "weekly":      weekly_pnl,
            "all_time":    all_time_pnl,
            "month_spend": month_spend,
            "spend_cap":   float(os.getenv("MONTHLY_SPEND_CAP", "100")),
            "by_agent":    by_agent,
            "expenses":    expenses,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Designs ──────────────────────────────────────────────────────────────────

@app.route("/designs")
def list_designs():
    designs_dir = ROOT / "designs"
    platform_filter = request.args.get("platform", "")

    try:
        q = (
            supabase.table("designs")
            .select("id, file_path, status, qa_reason, prompt_used, generation_cost, "
                    "created_at, niche, platform, variation_angle, attempts, brief_id")
            .order("created_at", desc=True)
            .limit(200)
        )
        if platform_filter:
            q = q.eq("platform", platform_filter)
        db_rows = _safe_data(q)
    except Exception as e:
        print(f"[/designs] DB query exception: {e}")
        db_rows = []

    def _norm_path(p: str) -> str:
        """Normalise any stored file_path to a relative forward-slash key.

        Supabase rows may contain absolute Windows paths
        (D:\\agenticsystems\\designs\\...) or already-relative paths
        (designs/...).  Strip the ROOT prefix if present so every key
        has the same form as rel_fwd computed from the filesystem walk.
        """
        p = p.replace("\\", "/")
        root_fwd = str(ROOT).replace("\\", "/").rstrip("/") + "/"
        if p.startswith(root_fwd):
            p = p[len(root_fwd):]
        # Also handle just a leading slash
        return p.lstrip("/")

    db_by_path = {_norm_path(r["file_path"]): r for r in db_rows if r.get("file_path")}

    results = []
    seen    = set()

    if designs_dir.exists():
        pngs = sorted(designs_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for png in pngs:
            rel      = png.relative_to(ROOT)
            rel_fwd  = str(rel).replace("\\", "/")
            meta: dict = {}
            mf = png.with_suffix(".json")
            if mf.exists():
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                except Exception:
                    pass

            parts    = png.relative_to(designs_dir).parts
            db       = db_by_path.get(rel_fwd, {})
            # Resolve platform: DB column is authoritative when present.
            # For sidecar-only files (not in DB), fall back to the first directory
            # segment under designs/ which is always 'fiverr' for Fiverr thumbnails
            # and the niche name for Etsy designs.
            first_seg     = parts[0].lower() if parts else ""
            path_platform = "fiverr" if first_seg == "fiverr" else "etsy"
            resolved_platform = db.get("platform") or path_platform

            if platform_filter and resolved_platform != platform_filter:
                continue

            print(f"[/designs] {png.name[:16]} db_status={db.get('status','(no match)')} has_file=True db_match={'yes' if db else 'NO'}")
            entry = {
                "url":             "/" + rel_fwd,
                "file_path":       rel_fwd,
                "platform":        resolved_platform,
                "niche":           db.get("niche") or meta.get("channel_niche") or (parts[0] if parts else "unknown"),
                "date":            parts[1] if len(parts) > 1 else "unknown",
                "filename":        png.name,
                "status":          db.get("status", "generated"),
                "qa_reason":       db.get("qa_reason") or "",
                "variation_angle": db.get("variation_angle") or "",
                "attempts":        db.get("attempts") or 1,
                "prompt_used":     meta.get("prompt_used") or db.get("prompt_used", ""),
                "generation_cost": float(meta.get("cost") or meta.get("generation_cost") or db.get("generation_cost") or 0),
                "timestamp":       meta.get("timestamp") or db.get("created_at", ""),
                "db_id":           db.get("id", ""),
                "has_file":        True,
                "missing":         False,
                # Fiverr-specific fields from sidecar
                "order_id":        meta.get("order_id", ""),
                "video_title":     meta.get("video_title", ""),
                "channel_niche":   meta.get("channel_niche", ""),
                "package":         meta.get("package", ""),
            }
            results.append(entry)
            seen.add(rel_fwd)

    # DB-only rows (file missing locally but exists in DB)
    for r in db_rows:
        if not r.get("file_path"):
            continue
        fp = r["file_path"].replace("\\", "/")
        if fp in seen:
            continue
        parts = Path(fp).parts
        # parts[0]='designs', parts[1]=niche-or-'fiverr'
        path_platform_db = "fiverr" if (len(parts) > 1 and parts[1].lower() == "fiverr") else "etsy"
        db_platform = r.get("platform") or path_platform_db
        results.append({
            "url":             "/" + fp,
            "file_path":       fp,
            "platform":        db_platform,
            "niche":           r.get("niche") or (parts[1] if len(parts) > 1 else "unknown"),
            "date":            parts[2] if len(parts) > 2 else "unknown",
            "filename":        Path(fp).name,
            "status":          r.get("status", "generated"),
            "qa_reason":       r.get("qa_reason") or "",
            "variation_angle": r.get("variation_angle") or "",
            "attempts":        r.get("attempts") or 1,
            "prompt_used":     r.get("prompt_used", ""),
            "generation_cost": float(r.get("generation_cost") or 0),
            "timestamp":       r.get("created_at", ""),
            "db_id":           r.get("id", ""),
            "has_file":        False,
            "missing":         True,
            "order_id":        "",
            "video_title":     "",
            "channel_niche":   "",
            "package":         "",
        })

    by_platform = {}
    for r in results:
        p = r.get("platform", "unknown")
        by_platform[p] = by_platform.get(p, 0) + 1
    if platform_filter:
        print(f"[/designs] filter={repr(platform_filter)} -> {len(results)} results {by_platform}")

    return jsonify(results)


# ─── Briefs ───────────────────────────────────────────────────────────────────

@app.route("/briefs")
def briefs():
    platform = request.args.get("platform", "")
    try:
        q = supabase.table("research_briefs").select("*").order("created_at", desc=True).limit(10)
        if platform:
            q = q.eq("platform", platform)
        data = _safe_data(q)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Scout proposals ──────────────────────────────────────────────────────────

@app.route("/proposals")
def list_proposals():
    status   = request.args.get("status", "")
    platform = request.args.get("platform", "")
    try:
        q = supabase.table("scout_proposals").select("*").order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        if platform == "fiverr":
            q = q.eq("platform", "fiverr_expansion")
        elif platform == "main":
            q = q.neq("platform", "fiverr_expansion")
        data = _safe_data(q)
        return jsonify(data)
    except Exception:
        return jsonify([])


@app.route("/designs/approve", methods=["POST"])
def design_approve():
    """Manual owner override — set a design to 'approved' and log the decision."""
    data      = request.get_json(silent=True) or {}
    design_id = (data.get("design_id") or "").strip()
    if not design_id:
        return jsonify({"error": "design_id required"}), 400
    try:
        update_design_status(design_id, "approved", "Manual owner override")
        try:
            from core.activity_logger import log_activity
            log_activity("owner", "system",
                         f"Manual override: design {design_id[:8]} set to approved by owner",
                         {"design_id": design_id, "new_status": "approved"})
        except Exception:
            pass
        return jsonify({"ok": True, "status": "approved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/designs/reject", methods=["POST"])
def design_reject():
    """Manual owner override — set a design to 'rejected' and log the decision."""
    data      = request.get_json(silent=True) or {}
    design_id = (data.get("design_id") or "").strip()
    if not design_id:
        return jsonify({"error": "design_id required"}), 400
    try:
        update_design_status(design_id, "rejected", "Manual owner override")
        try:
            from core.activity_logger import log_activity
            log_activity("owner", "system",
                         f"Manual override: design {design_id[:8]} set to rejected by owner",
                         {"design_id": design_id, "new_status": "rejected"})
        except Exception:
            pass
        return jsonify({"ok": True, "status": "rejected"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/proposals/approve", methods=["POST"])
def approve_proposal_route():
    from core.supabase_client import approve_proposal
    data        = request.get_json(silent=True) or {}
    proposal_id = (data.get("proposal_id") or "").strip()
    if not proposal_id:
        return jsonify({"error": "proposal_id required"}), 400
    try:
        row = approve_proposal(proposal_id)
        return jsonify({"ok": True, "status": row.get("status")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/proposals/ignore", methods=["POST"])
def ignore_proposal_route():
    from core.supabase_client import ignore_proposal
    data        = request.get_json(silent=True) or {}
    proposal_id = (data.get("proposal_id") or "").strip()
    if not proposal_id:
        return jsonify({"error": "proposal_id required"}), 400
    try:
        row = ignore_proposal(proposal_id)
        return jsonify({"ok": True, "status": row.get("status")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Scheduler conditions ─────────────────────────────────────────────────────

def _conditions_with_timeout(timeout_s: float = 3.0) -> dict:
    """
    Run each condition check in the same thread but wrap every individual call
    in try/except so one slow or broken check never blocks the whole endpoint.
    Falls back to should_run=True with an error reason if a check throws.
    """
    import concurrent.futures
    from core import scheduler_conditions as sc

    checks = {
        "research":         sc.check_research,
        "design":           sc.check_design,
        "publisher":        sc.check_publisher,
        "fiverr_orders":    sc.check_fiverr_orders,
        "performance":      sc.check_performance,
        "memory":           sc.check_memory,
        "anomaly":          sc.check_anomaly,
        "scout":            sc.check_scout,
        "reporting":        sc.check_reporting,
        "prompt_evolution": sc.check_prompt_evolution,
    }

    result: dict = {}

    # Run all checks in a thread pool with a shared deadline
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): name for name, fn in checks.items()}
        done, _ = concurrent.futures.wait(futures, timeout=timeout_s)

        for fut, name in futures.items():
            if fut in done:
                try:
                    result[name] = fut.result()
                except Exception as e:
                    result[name] = {
                        "should_run": True,
                        "reason":     f"check error: {e}",
                        "extra":      {},
                    }
            else:
                # Timed out
                result[name] = {
                    "should_run": True,
                    "reason":     "condition check timed out — running to be safe",
                    "extra":      {},
                }

    from datetime import datetime, timezone
    result["_checked_at"] = datetime.now(timezone.utc).isoformat()
    return result


@app.route("/scheduler/conditions")
def scheduler_conditions():
    try:
        return jsonify(_conditions_with_timeout(3.0))
    except Exception as e:
        return jsonify({"error": str(e), "_checked_at": datetime.now(timezone.utc).isoformat()}), 500


# ─── Activity log ──────────────────────────────────────────────────────────────

@app.route("/activity")
def activity_log():
    limit      = min(int(request.args.get("limit", 50)), 200)
    event_type = request.args.get("event_type", "")
    try:
        q = (
            supabase.table("activity_log")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if event_type:
            q = q.eq("event_type", event_type)
        return jsonify(_safe_data(q))
    except Exception as e:
        return jsonify([])


# ─── Notifications (derived from activity_log) ────────────────────────────────

_NOTIFICATION_TYPES = {"error", "proposal_found", "order_received", "sale"}

@app.route("/notifications")
def notifications():
    """Return recent alert-worthy activity entries (last 24 h, max 20)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        rows = _safe_data(
            supabase.table("activity_log")
            .select("*")
            .in_("event_type", list(_NOTIFICATION_TYPES))
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(20)
        )
        return jsonify(rows)
    except Exception:
        return jsonify([])


# ─── Orchestrator ─────────────────────────────────────────────────────────────

@app.route("/orchestrator", methods=["POST"])
def orchestrator_chat():
    from agents.orchestrator_agent import chat as _orch_chat
    data    = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    if not message:
        return jsonify({"error": "message is required"}), 400
    try:
        result = _orch_chat(message, history)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ─── /set-env ─────────────────────────────────────────────────────────────────

_ALLOWED_ENV_KEYS = {
    "PRINTIFY_API_KEY", "PRINTIFY_SHOP_ID",
    "PRINTIFY_BLUEPRINT_ID", "PRINTIFY_PROVIDER_ID",
    "FIVERR_USERNAME", "FIVERR_NOTIFICATION_EMAIL",
    "GMAIL_APP_PASSWORD", "GMAIL_IMAP_SERVER", "GMAIL_IMAP_PORT",
    "SENDGRID_API_KEY", "REPORT_EMAIL",
    "DRAFT_MODE", "MONTHLY_SPEND_CAP", "DAILY_DESIGN_TARGET",
    "DAILY_LISTING_CAP", "LISTING_SPACING_MINUTES",
    "ANTHROPIC_MODEL", "MOCK_ETSY", "MOCK_FIVERR", "MOCK_SCOUT",
}


@app.route("/set-env", methods=["POST"])
def set_env():
    remote = request.remote_addr
    if remote not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "Forbidden - localhost only"}), 403
    data  = request.get_json(silent=True) or {}
    key   = (data.get("key") or "").strip()
    value = data.get("value", "")
    if not key:
        return jsonify({"error": "key is required"}), 400
    if key not in _ALLOWED_ENV_KEYS:
        return jsonify({"error": f"Key '{key}' is not in the allowed list"}), 400
    env_path = ROOT / ".env"
    try:
        original = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    lines    = original.splitlines()
    updated  = False
    new_lines: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip() == key:
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.environ[key] = str(value)
    return jsonify({"ok": True, "key": key, "updated": updated})


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[dashboard] Starting — {__file__}")
    print("[dashboard] http://localhost:5000")
    app.run(debug=False, port=5000, threaded=True)
