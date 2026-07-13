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
                                           is_active INTEGER DEFAULT 0
);