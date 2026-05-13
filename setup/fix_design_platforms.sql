-- fix_design_platforms.sql
-- Run once in Supabase SQL Editor.
-- Fixes the platform column on the designs table for rows that have the wrong
-- platform value.  Handles both Windows backslash and forward-slash file paths.

-- Mark Fiverr thumbnails (stored under designs/fiverr/... or designs\fiverr\...)
UPDATE designs
SET platform = 'fiverr'
WHERE file_path LIKE '%/fiverr/%'
   OR file_path LIKE '%\fiverr\%';

-- Ensure known Etsy niches are marked correctly (platform stays 'etsy')
UPDATE designs
SET platform = 'etsy'
WHERE (
    file_path LIKE '%electrician%'
 OR file_path LIKE '%nurse%'
 OR file_path LIKE '%teacher%'
)
AND platform = 'fiverr';   -- only correct accidental fiverr tagging

-- Verify counts after running:
SELECT platform, count(*) FROM designs GROUP BY platform;
