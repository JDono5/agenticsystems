-- ============================================================
-- new_tables.sql  — Run this once in the Supabase SQL Editor
-- Adds: expenses, memory, job_queue, scout_proposals
-- Safe to run multiple times (IF NOT EXISTS everywhere)
-- ============================================================

-- ─── expenses ──────────────────────────────────────────────
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

-- Seed: Etsy $29 shop opening fee (idempotent)
INSERT INTO expenses (expense_date, category, platform, description, amount_usd, recurring)
SELECT CURRENT_DATE, 'platform_fee', 'etsy', 'Etsy shop opening fee', 29.00, false
WHERE NOT EXISTS (
  SELECT 1 FROM expenses WHERE description = 'Etsy shop opening fee'
);

-- ─── memory ────────────────────────────────────────────────
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

-- ─── job_queue ─────────────────────────────────────────────
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

-- ─── scout_proposals ───────────────────────────────────────
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
