-- AuctionScout schema
-- Pattern: stable property identity + current auction state + append-only event log

CREATE TABLE IF NOT EXISTS properties (
    property_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source                TEXT NOT NULL,          -- 'sullivan', 'harmon_law', etc.
    source_listing_id     TEXT NOT NULL,          -- extracted from source URL (?id=21007)
    address_raw           TEXT NOT NULL,
    latitude              REAL,
    longitude             REAL,
    state                 TEXT,
    county                TEXT,
    municipality          TEXT,
    first_seen_at         TEXT NOT NULL,          -- ISO8601, first spider run that found it
    last_seen_at          TEXT NOT NULL,          -- updated every run it's still present in
    UNIQUE(source, source_listing_id)
    );

CREATE TABLE IF NOT EXISTS auctions (
                                        auction_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                                        property_id           INTEGER NOT NULL REFERENCES properties(property_id),
    auction_datetime_raw  TEXT,                   -- original string, e.g. "Wed. Jul. 8, 2026 at 11 am"
    auction_datetime      TEXT,                   -- parsed ISO8601, nullable if unparseable
    status                TEXT NOT NULL,          -- on|postponed|canceled|sold
    description_raw       TEXT,
    property_type         TEXT,
    bedrooms              INTEGER,
    bathrooms             REAL,
    sqft                  INTEGER,
    lot_size_raw          TEXT,                 -- kept raw: mixes acres/sf across listings
    year_built            INTEGER,
    mortgage_ref          TEXT,
    source_url            TEXT NOT NULL,
    last_updated_at       TEXT NOT NULL,
    UNIQUE(property_id)   -- one "current" auction row per property; history lives in auction_events
    );

CREATE TABLE IF NOT EXISTS auction_pdf_links (
    link_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id    INTEGER NOT NULL REFERENCES auctions(auction_id),
    url           TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS auction_events (
                                              event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                                              auction_id      INTEGER NOT NULL REFERENCES auctions(auction_id),
    event_type       TEXT NOT NULL,     -- first_seen|status_change|date_change|price_change
    old_value        TEXT,
    new_value        TEXT,
    detected_at      TEXT NOT NULL,
    spider_run_id    INTEGER REFERENCES spider_runs(run_id)
    );

CREATE TABLE IF NOT EXISTS spider_runs (
   run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
   source            TEXT NOT NULL,
   started_at        TEXT NOT NULL,
   finished_at       TEXT,
   records_found     INTEGER,
   records_new       INTEGER,
   records_changed   INTEGER,
   status            TEXT               -- success|failed|partial
);

CREATE TABLE IF NOT EXISTS property_duplicate_links (
    link_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id             INTEGER NOT NULL REFERENCES properties(property_id), -- the one to HIDE from the map
    canonical_property_id   INTEGER NOT NULL REFERENCES properties(property_id), -- the one to SHOW on the map
    match_distance_m        REAL,     -- geographic distance between the pair, meters
    match_score             REAL,     -- normalized address similarity, 0..1
    detected_at             TEXT NOT NULL,
    UNIQUE(property_id)
    );

CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status);
CREATE INDEX IF NOT EXISTS idx_auctions_datetime ON auctions(auction_datetime);
CREATE INDEX IF NOT EXISTS idx_properties_state ON properties(state);