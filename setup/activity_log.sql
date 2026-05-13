-- activity_log table — persistent event feed for the dashboard
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run

CREATE TABLE IF NOT EXISTS activity_log (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at  timestamptz NOT NULL DEFAULT now(),
  agent       text        NOT NULL,
  event_type  text        NOT NULL
                CHECK (event_type IN (
                  'design_generated', 'design_approved', 'design_rejected',
                  'order_received',   'order_fulfilled',  'listing_published',
                  'research_complete','error',             'sale',
                  'proposal_found',   'system'
                )),
  message     text        NOT NULL,
  metadata    jsonb
);

CREATE INDEX IF NOT EXISTS idx_activity_created    ON activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_event_type ON activity_log(event_type);
CREATE INDEX IF NOT EXISTS idx_activity_agent      ON activity_log(agent);

-- Keep the table lean — auto-purge entries older than 30 days (run via cron or
-- add a pg_cron job if available on your Supabase tier).
-- Manual purge: DELETE FROM activity_log WHERE created_at < NOW() - INTERVAL '30 days';
