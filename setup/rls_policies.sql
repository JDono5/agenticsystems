-- RLS Policies for AI Agent Passive Income System
-- Run this in: Supabase Dashboard → SQL Editor → New Query → Run
-- This allows the service role (used by all agents) to read/write all tables.
-- The anon key is intentionally NOT granted write access — agents use service_role only.

-- ─── research_briefs ──────────────────────────────────────────────────────────
CREATE POLICY "service_role full access" ON research_briefs
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ─── designs ──────────────────────────────────────────────────────────────────
CREATE POLICY "service_role full access" ON designs
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ─── listings ─────────────────────────────────────────────────────────────────
CREATE POLICY "service_role full access" ON listings
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ─── cost_log ─────────────────────────────────────────────────────────────────
CREATE POLICY "service_role full access" ON cost_log
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ─── sales ────────────────────────────────────────────────────────────────────
CREATE POLICY "service_role full access" ON sales
    FOR ALL TO service_role USING (true) WITH CHECK (true);
