-- add_sales_platform.sql
-- Run once in Supabase SQL Editor to add platform support to the sales table.
-- Safe to re-run (IF NOT EXISTS).

ALTER TABLE sales
  ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'etsy';

CREATE INDEX IF NOT EXISTS idx_sales_platform ON sales(platform);

-- Back-fill: any existing rows default to 'etsy' (already done by the DEFAULT above)
