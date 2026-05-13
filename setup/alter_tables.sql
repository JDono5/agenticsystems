-- ============================================================
-- alter_tables.sql — Run ONCE in Supabase SQL Editor
-- Upgrades original schema to v5.0 spec column set
-- Safe to re-run: uses ADD COLUMN IF NOT EXISTS everywhere
-- ============================================================

-- ─── research_briefs — add v5 columns ────────────────────────────────────────
ALTER TABLE research_briefs
  ADD COLUMN IF NOT EXISTS platform                    text NOT NULL DEFAULT 'etsy',
  ADD COLUMN IF NOT EXISTS recommended_design_direction text,
  ADD COLUMN IF NOT EXISTS recommended_price           numeric(10,2),
  ADD COLUMN IF NOT EXISTS avg_competitor_price        numeric(10,2),
  ADD COLUMN IF NOT EXISTS competitor_title_examples   text[] DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS raw_scrape_data             jsonb,
  ADD COLUMN IF NOT EXISTS tokens_used                 integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cost_usd                    numeric(10,6) DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_briefs_platform ON research_briefs(platform);
CREATE INDEX IF NOT EXISTS idx_briefs_created  ON research_briefs(created_at DESC);

-- ─── designs — add v5 columns ────────────────────────────────────────────────
ALTER TABLE designs
  ADD COLUMN IF NOT EXISTS platform          text NOT NULL DEFAULT 'etsy',
  ADD COLUMN IF NOT EXISTS niche             text NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS variation_angle   text,
  ADD COLUMN IF NOT EXISTS qa_cost           numeric(10,6) DEFAULT 0,
  ADD COLUMN IF NOT EXISTS qa_reason         text,
  ADD COLUMN IF NOT EXISTS attempts          integer DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_designs_status   ON designs(status);
CREATE INDEX IF NOT EXISTS idx_designs_platform ON designs(platform);
CREATE INDEX IF NOT EXISTS idx_designs_created  ON designs(created_at DESC);

-- ─── listings — add v5 columns ───────────────────────────────────────────────
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS platform              text NOT NULL DEFAULT 'etsy',
  ADD COLUMN IF NOT EXISTS external_listing_id   text,
  ADD COLUMN IF NOT EXISTS description           text,
  ADD COLUMN IF NOT EXISTS price                 numeric(10,2),
  ADD COLUMN IF NOT EXISTS impressions           integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS clicks                integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS niche                 text,
  ADD COLUMN IF NOT EXISTS last_optimized_at     timestamptz;

CREATE INDEX IF NOT EXISTS idx_listings_platform ON listings(platform);
CREATE INDEX IF NOT EXISTS idx_listings_status   ON listings(status);

-- ─── cost_log — add notes column (used by several agents) ────────────────────
ALTER TABLE cost_log
  ADD COLUMN IF NOT EXISTS notes text;
