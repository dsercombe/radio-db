-- Add link tracking tables for tracked redirect links and click events
-- Run this against Postgres (Supabase) using your migration tooling or `psql`.

CREATE TABLE IF NOT EXISTS link_tracking_codes (
    id SERIAL PRIMARY KEY,
    code VARCHAR(120) UNIQUE NOT NULL,
    campaign_id INTEGER REFERENCES outreach_campaigns(id) ON DELETE SET NULL,
    draft_id INTEGER REFERENCES contact_drafts(id) ON DELETE SET NULL,
    send_id INTEGER REFERENCES contact_sends(id) ON DELETE SET NULL,
    recipient_email VARCHAR(255),
    link_type VARCHAR(64) NOT NULL DEFAULT 'website',
    original_url TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_link_tracking_codes_code ON link_tracking_codes(code);
CREATE INDEX IF NOT EXISTS idx_link_tracking_codes_campaign ON link_tracking_codes(campaign_id);

CREATE TABLE IF NOT EXISTS link_clicks (
    id SERIAL PRIMARY KEY,
    tracking_code_id INTEGER REFERENCES link_tracking_codes(id) ON DELETE CASCADE,
    ip_address VARCHAR(64),
    user_agent TEXT,
    referer TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_link_clicks_tracking_code ON link_clicks(tracking_code_id);
