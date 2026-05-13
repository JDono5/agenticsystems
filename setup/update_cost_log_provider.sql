-- Update cost_log provider constraint to allow 'google' (Imagen 3)
-- Run in: Supabase Dashboard → SQL Editor

ALTER TABLE cost_log
  DROP CONSTRAINT IF EXISTS cost_log_provider_check;

ALTER TABLE cost_log
  ADD CONSTRAINT cost_log_provider_check
  CHECK (provider IN ('anthropic', 'openai', 'google'));
