-- AI Agent Passive Income System — Supabase Schema
-- Run this entire file in: Supabase Dashboard → SQL Editor → New Query → Run
-- Safe to re-run: all statements use CREATE TABLE IF NOT EXISTS

-- ─── 1. research_briefs ───────────────────────────────────────────────────────
-- Written by research_agent.py each morning. Read by design_agent.py.
CREATE TABLE IF NOT EXISTS research_briefs (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    niche                TEXT NOT NULL,
    sub_niche            TEXT NOT NULL,
    top_keywords         TEXT[] NOT NULL DEFAULT '{}',
    opportunity_summary  TEXT,
    top_competitor_titles TEXT[] NOT NULL DEFAULT '{}',
    avg_price_point      NUMERIC(10, 2)
);

-- ─── 2. designs ───────────────────────────────────────────────────────────────
-- Written by design_agent.py. Status progresses: generated → approved/rejected → published
CREATE TABLE IF NOT EXISTS designs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    brief_id         UUID NOT NULL REFERENCES research_briefs(id) ON DELETE CASCADE,
    file_path        TEXT,
    prompt_used      TEXT,
    generation_cost  NUMERIC(10, 6) NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'generated'
                     CHECK (status IN ('generated', 'approved', 'rejected', 'published'))
);

-- ─── 3. listings ──────────────────────────────────────────────────────────────
-- Written by publisher_agent.py after pushing to Etsy/Printify.
CREATE TABLE IF NOT EXISTS listings (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    design_id            UUID NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
    etsy_listing_id      TEXT,
    printify_product_id  TEXT,
    title                TEXT,
    tags                 TEXT[] NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft', 'active', 'paused', 'removed')),
    published_at         TIMESTAMPTZ
);

-- ─── 4. cost_log ──────────────────────────────────────────────────────────────
-- Every API call is logged here immediately. spend_monitor.py reads this table.
CREATE TABLE IF NOT EXISTS cost_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    agent        TEXT NOT NULL,
    provider     TEXT NOT NULL CHECK (provider IN ('anthropic', 'openai', 'google')),
    model        TEXT NOT NULL,
    tokens_used  INTEGER NOT NULL DEFAULT 0,
    cost_usd     NUMERIC(10, 6) NOT NULL DEFAULT 0
);

-- ─── 5. sales ─────────────────────────────────────────────────────────────────
-- Populated by reporting_agent.py when it syncs orders from Etsy.
CREATE TABLE IF NOT EXISTS sales (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_date       TIMESTAMPTZ NOT NULL,
    etsy_order_id    TEXT UNIQUE NOT NULL,
    listing_id       UUID REFERENCES listings(id) ON DELETE SET NULL,
    gross_revenue    NUMERIC(10, 2) NOT NULL DEFAULT 0,
    printify_cost    NUMERIC(10, 2) NOT NULL DEFAULT 0,
    etsy_fee         NUMERIC(10, 2) NOT NULL DEFAULT 0,
    net_profit       NUMERIC(10, 2) GENERATED ALWAYS AS
                     (gross_revenue - printify_cost - etsy_fee) STORED
);

-- ─── Indexes for common query patterns ────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_designs_brief_id     ON designs(brief_id);
CREATE INDEX IF NOT EXISTS idx_designs_status       ON designs(status);
CREATE INDEX IF NOT EXISTS idx_listings_design_id   ON listings(design_id);
CREATE INDEX IF NOT EXISTS idx_cost_log_timestamp   ON cost_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_sales_listing_id     ON sales(listing_id);
CREATE INDEX IF NOT EXISTS idx_sales_order_date     ON sales(order_date);

-- ─── expenses ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS expenses (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  expense_date        date NOT NULL DEFAULT CURRENT_DATE,
  category            text NOT NULL
                        CHECK (category IN ('platform_fee','fulfillment','subscription','advertising','other')),
  platform            text NOT NULL,
  description         text NOT NULL,
  amount_usd          numeric(10,2) NOT NULL,
  recurring           boolean NOT NULL DEFAULT false,
  recurring_interval  text CHECK (recurring_interval IN ('monthly','annual',NULL))
);

-- Seed: Etsy shop opening fee (log it now so all-time P&L is accurate from day 1)
INSERT INTO expenses (expense_date, category, platform, description, amount_usd, recurring)
SELECT CURRENT_DATE, 'platform_fee', 'etsy', 'Etsy shop opening fee', 29.00, false
WHERE NOT EXISTS (
  SELECT 1 FROM expenses WHERE description = 'Etsy shop opening fee'
);

-- ─── memory ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_updated  timestamptz NOT NULL DEFAULT now(),
  category      text NOT NULL
                  CHECK (category IN ('design_quality','niche_performance','platform_health',
                                      'agent_reliability','prompt_performance','scout_findings')),
  key           text NOT NULL UNIQUE,
  value         jsonb NOT NULL,
  confidence    numeric(3,2) DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  sample_size   integer DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_memory_category ON memory(category);
CREATE INDEX IF NOT EXISTS idx_memory_key      ON memory(key);

-- ─── job_queue ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS job_queue (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  job_type        text NOT NULL,
  platform        text,
  payload         jsonb NOT NULL DEFAULT '{}',
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','processing','done','failed')),
  picked_up_at    timestamptz,
  completed_at    timestamptz,
  error_message   text
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON job_queue(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type   ON job_queue(job_type);

-- ─── scout_proposals ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scout_proposals (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at              timestamptz NOT NULL DEFAULT now(),
  opportunity_name        text NOT NULL,
  platform                text NOT NULL,
  how_it_works            text NOT NULL,
  agent_needed            text NOT NULL,
  setup_time_hours        numeric(4,1),
  monthly_potential_usd   numeric(10,2),
  risk_description        text,
  status                  text NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','approved','ignored','launched')),
  approved_at             timestamptz,
  launched_at             timestamptz,
  credential_required     boolean DEFAULT false,
  credential_instructions text
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON scout_proposals(status);
