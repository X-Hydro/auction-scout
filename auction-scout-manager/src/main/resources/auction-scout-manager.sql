CREATE TABLE IF NOT EXISTS login_tokens (
  token TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  used INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_login_tokens_email ON login_tokens(email);

CREATE TABLE IF NOT EXISTS subscribers (
                                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                                           email TEXT NOT NULL UNIQUE,
                                           created_at INTEGER NOT NULL,
                                           verified_at INTEGER,
                                           is_active INTEGER DEFAULT 0,
                                           session_token TEXT UNIQUE,
                                           username TEXT,
    -- Both nullable: subscription_start_date is set the first time the
    -- subscriber completes their profile (not at registration/verify
    -- time, which only proves email ownership). subscription_end_date
    -- stays NULL for the free period — once Stripe is added, this is
    -- where a paid period's end would be recorded.
                                           subscription_start_date INTEGER,
                                           subscription_end_date INTEGER,
                                           email_alerts_enabled INTEGER NOT NULL DEFAULT 1,
    -- Trial length (30 days from subscription_start_date) is computed at
    -- query time, not stored — subscription_start_date stays the single
    -- source of truth for "when did this subscriber's clock start."
    -- These three are NULL until a subscriber ever starts Stripe
    -- checkout; stripe_subscription_status mirrors Stripe's own status
    -- string ('active', 'past_due', 'canceled', ...) rather than a
    -- boolean, so a webhook can just copy the field it received without
    -- this app needing to reinterpret Stripe's state machine.
                                           stripe_customer_id TEXT,
                                           stripe_subscription_id TEXT,
                                           stripe_subscription_status TEXT
);

CREATE INDEX IF NOT EXISTS idx_subscribers_session_token ON subscribers(session_token);

-- Max 4 states per subscriber is enforced in SubscriberRepository, not
-- here — SQLite has no clean way to express "at most N rows per
-- subscriber_id" as a table constraint.
CREATE TABLE IF NOT EXISTS subscriber_states (
  subscriber_id INTEGER NOT NULL,
  state TEXT NOT NULL,
  PRIMARY KEY (subscriber_id, state),
  FOREIGN KEY (subscriber_id) REFERENCES subscribers(id)
);

CREATE TABLE IF NOT EXISTS email_notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER REFERENCES subscribers(id),
    email TEXT NOT NULL,
    notification_type TEXT NOT NULL, -- 'welcome' | 'weekly' | 'test'
    sent_at INTEGER NOT NULL
    );

CREATE INDEX IF NOT EXISTS idx_email_notifications_email_sent_at
    ON email_notifications(email, sent_at);

-- Stripe redelivers a webhook whenever it doesn't get a clean 2xx back
-- in time (network blip, slow handler, etc.), even if the first
-- delivery actually succeeded server-side. This table lets
-- StripeWebhookController recognize "I've already handled this exact
-- event.id" and skip re-applying it, rather than e.g. calling
-- recordStripeSubscription() twice for one checkout.
CREATE TABLE IF NOT EXISTS stripe_webhook_events (
     event_id TEXT PRIMARY KEY,
     processed_at INTEGER NOT NULL
);