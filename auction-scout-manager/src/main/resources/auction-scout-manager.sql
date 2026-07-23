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
  username TEXT NOT NULL,
  subscription_start_date INTEGER,
  subscription_end_date INTEGER,
  email_alerts_enabled INTEGER NOT NULL DEFAULT 1,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  stripe_subscription_status TEXT
);

CREATE INDEX IF NOT EXISTS idx_subscribers_session_token ON subscribers(session_token);

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
     processed_at INTEGER NOT NULL,
     event_type TEXT,
     email TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL REFERENCES subscribers(email),
  invoice_date INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL DEFAULT 0,
  status TEXT DEFAULT 'paid',
  description TEXT,
  created_at INTEGER NOT NULL,
  payment_reference TEXT,
  payment_last4 TEXT,
  stripe_invoice_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS saved_properties (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL REFERENCES subscribers(email),
  property_id TEXT NOT NULL,
  address_raw TEXT NOT NULL,
  state TEXT NOT NULL,
  county TEXT,
  municipality TEXT,
  latitude REAL,
  longitude REAL,
  saved_at INTEGER NOT NULL,
  UNIQUE(email, property_id)
);

