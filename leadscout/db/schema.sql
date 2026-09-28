-- LeadScout SQLite schema. Applied with CREATE ... IF NOT EXISTS on every
-- start, so it is safe to run against an existing database.

-- Every search or enrich job the user starts
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'search',  -- search | enrich
    industries      TEXT NOT NULL,          -- JSON list
    location_input  TEXT NOT NULL,          -- what the user typed
    location_type   TEXT NOT NULL,          -- place | radius | batch | csv
    sources         TEXT NOT NULL,          -- JSON list
    settings        TEXT NOT NULL,          -- JSON snapshot of filters/specs
    status          TEXT NOT NULL,          -- pending | running | paused | done | failed
    stage           TEXT,                   -- stage currently/last running
    stats           TEXT,                   -- JSON counts per stage
    error           TEXT
);

-- Discovery work units: one per (location, industry) pair of a run
CREATE TABLE IF NOT EXISTS run_tasks (
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    location        TEXT NOT NULL,          -- JSON location spec
    industry        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | skipped
    found           INTEGER DEFAULT 0,
    error           TEXT,
    PRIMARY KEY (run_id, location, industry)
);

-- One row per unique business (deduped across all runs)
CREATE TABLE IF NOT EXISTS businesses (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,
    industry        TEXT,
    address         TEXT,
    city            TEXT,
    region          TEXT,
    postcode        TEXT,
    country         TEXT,                   -- ISO 3166-1 alpha-2
    lat             REAL,
    lng             REAL,
    phone           TEXT,                   -- E.164
    website         TEXT,
    domain          TEXT,
    website_status  TEXT,                   -- none | ok | social_only
    final_url       TEXT,
    socials         TEXT,                   -- JSON {network: url}
    platform        TEXT,                   -- shopify | wordpress | ...
    tags            TEXT,                   -- JSON: brand + selected source tags
    location_count  INTEGER NOT NULL DEFAULT 1,
    multi_location_site INTEGER DEFAULT 0,
    intl_languages  INTEGER,                -- language/country versions of the website
    investor_relations INTEGER DEFAULT 0,   -- site has an investor relations section
    org_info        TEXT,
    listing_updated TEXT,                   -- date the map's data provider last updated the listing
    listing_provider TEXT,                  -- Foursquare, meta, microsoft ...
    latest_date_year INTEGER,               -- newest year in a real date on the website
    closed_notice   TEXT,                   -- website says closed / parked / suspended
    redirected_to   TEXT,                   -- website now goes to an unrelated domain (sold, merged, taken over)
    activity        TEXT,                   -- active | likely active | unclear | likely closed
    activity_reason TEXT,
    people          TEXT,                   -- JSON [{name, title, email, level}] named on the website, most senior first
    owner_name      TEXT,                   -- owner / decision maker named on the website
    owner_title     TEXT,
    owner_basis     TEXT,                   -- "title on website" | "business named after them"                   -- JSON company-size evidence (Wikidata); NULL = not checked
    copyright_year  INTEGER,
    team_size_hint  INTEGER,
    has_careers     INTEGER DEFAULT 0,
    is_chain        INTEGER DEFAULT 0,
    chain_reason    TEXT,
    score           INTEGER,
    score_detail    TEXT,                   -- JSON breakdown (best email)
    stage           TEXT NOT NULL,          -- found | crawled | verified | scored
    error           TEXT,
    first_seen      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_business_domain ON businesses(domain) WHERE domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_business_phone ON businesses(phone);
CREATE INDEX IF NOT EXISTS idx_business_stage ON businesses(stage);
CREATE INDEX IF NOT EXISTS idx_business_name ON businesses(name_norm);

-- Which businesses belong to which run
CREATE TABLE IF NOT EXISTS run_businesses (
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    industry        TEXT,
    location        TEXT,
    PRIMARY KEY (run_id, business_id)
);

-- Where each business came from (a business can have several sources)
CREATE TABLE IF NOT EXISTS business_sources (
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    run_id          INTEGER REFERENCES runs(id),
    source          TEXT NOT NULL,          -- overture | csv
    source_ref      TEXT NOT NULL,          -- Overture place ID, file:row
    PRIMARY KEY (source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_sources_business ON business_sources(business_id);

-- Emails found for businesses
CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    email           TEXT NOT NULL UNIQUE,
    email_type      TEXT,                   -- personal | generic | role
    source          TEXT,                   -- website | overture | csv
    found_on_url    TEXT,
    is_freemail     INTEGER DEFAULT 0,
    contact_name    TEXT,                   -- person behind the address, from the website
    designation     TEXT,                   -- their job title, or the inbox's role (Sales, Billing ...)
    decision_maker  INTEGER DEFAULT 0,      -- owner / founder / CEO / president / managing partner
    designation_source TEXT,                -- website | address
    status          TEXT,                   -- valid | risky | invalid | unknown (NULL = not verified)
    status_reason   TEXT,
    is_catch_all    INTEGER,
    mailbox_check   TEXT,
    seen_at         TEXT,                   -- date the address was last seen (website crawl or listing date)                   -- mail server's answer: valid | invalid | risky | unknown (NULL = not asked)
    score           INTEGER,
    score_detail    TEXT,
    verified_at     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emails_business ON emails(business_id);
CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);

-- Pages fetched while crawling
CREATE TABLE IF NOT EXISTS crawl_pages (
    url             TEXT PRIMARY KEY,
    domain          TEXT,
    http_status     INTEGER,
    fetched_at      TEXT,
    error           TEXT
);

-- Do-not-contact list (emails or whole domains)
CREATE TABLE IF NOT EXISTS suppression (
    value           TEXT PRIMARY KEY,       -- email or domain
    kind            TEXT NOT NULL,          -- email | domain
    reason          TEXT,                   -- unsubscribed | bounced | manual
    added_at        TEXT NOT NULL
);

-- Record of every export, so "only new leads" works
CREATE TABLE IF NOT EXISTS exports (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    filters         TEXT NOT NULL,          -- JSON
    file_path       TEXT NOT NULL,
    row_count       INTEGER
);
CREATE TABLE IF NOT EXISTS export_items (
    export_id       INTEGER NOT NULL REFERENCES exports(id),
    email_id        INTEGER NOT NULL REFERENCES emails(id),
    PRIMARY KEY (export_id, email_id)
);
CREATE INDEX IF NOT EXISTS idx_export_items_email ON export_items(email_id);

-- Caches and API usage tracking
CREATE TABLE IF NOT EXISTS geocode_cache (
    query           TEXT PRIMARY KEY,
    result          TEXT NOT NULL,
    cached_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS org_cache (
    domain          TEXT PRIMARY KEY,
    result          TEXT NOT NULL,          -- JSON company-size evidence, {} = nothing found
    checked_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS smtp_cache (
    domain          TEXT PRIMARY KEY,
    result          TEXT NOT NULL,          -- JSON {email: [status, reason]} from the mail server
    checked_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dns_cache (
    domain          TEXT PRIMARY KEY,
    result          TEXT NOT NULL,          -- JSON {status, reason}
    checked_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_usage (
    month           TEXT NOT NULL,          -- YYYY-MM
    api             TEXT NOT NULL,          -- google_places
    requests        INTEGER DEFAULT 0,
    PRIMARY KEY (month, api)
);
