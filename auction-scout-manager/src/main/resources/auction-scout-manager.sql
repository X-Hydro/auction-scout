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
  subscription_end_date INTEGER
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