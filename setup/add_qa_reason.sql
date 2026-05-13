-- Add qa_reason column to designs table
-- Run this once in the Supabase SQL editor if the column doesn't exist yet.

ALTER TABLE designs ADD COLUMN IF NOT EXISTS qa_reason text;
