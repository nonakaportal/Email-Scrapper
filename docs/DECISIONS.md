# Decision log

Newest first. Each entry records what was decided and why.

## 2026-09-28: Mailbox check (full verification)

- **What it does:** asks each company's mail server (RCPT TO) whether the address exists, then
  quits without sending anything. One connection per email domain checks all of that domain's
  addresses plus one random address. If the random address is accepted, the domain is a
  catch-all, and its "yes" answers are downgraded to risky.
- **What the answers mean:**
  - 5.1.x or "no such user" wording: invalid.
  - 5.7.x, or blocklist/policy wording: unknown. It is about our IP, not the mailbox.
  - An unknown answer never overrides the basic result. The email keeps "valid (not confirmed)".
    A wrong "invalid" would throw away a good lead.
- **Sender:** `MAIL FROM:<>` (the null sender that mail servers use for their own callouts), unless
  `email.smtp_from_domain` is set. The user's contact email is never used, because it would be
  handed to strangers' mail servers.
- **Politeness:**
  - Free-mail providers are skipped.
  - 6 connections per minute per provider. Providers are grouped by the last two labels of the MX
    host, so all Google Workspace domains share one limit.
  - 8 threads; database writes stay on the run's thread.
  - Answers are cached for 14 days, and transient failures are not cached.
- **When it runs:** it is a per-run choice (`verification: basic|full` in the run settings). The
  default comes from `email.smtp_check`, which is false. Old runs can be checked later with
  "Check mailboxes" / `leadscout mailboxes`, which also retries unknown answers.
- **Port 25:** tested once per run against Gmail's MX. If it is blocked, the step is skipped with
  a note.
- **Live test from a home connection** (Boulder dental run, 10 company addresses):
  - 3 confirmed.
  - 2 catch-alls.
  - 5 refused: Microsoft 365 and Paubox reject IPs on Spamhaus's list of home connections.
  - All 5 were correctly kept as "not confirmed", not invalid.

## 2026-09-28: Names, designations and owners for every contact

- **Asked for:** one lead per company, with colleagues listed as other contacts, each with their designation.
- **Where names come from:**
  - Only the business's own pages already crawled: schema.org Person data, "Name - Title" /
    "Title: Name" lines, name-then-title cards, "Dr. First Last" in text, and first-name-only
    staff lines ("Devin – Office Manager").
  - An email is linked to a person when it is listed next to them or named like them (anna@,
    alopez@, craiglarsondds@). The link is made only when exactly one person fits.
  - Shared inboxes get a role from the address.
  - No guessing and no outside people databases. An empty designation is better than a wrong one.
- **Decision makers:**
  - Owner, founder, CEO, president, principal, managing partner/member/director.
  - Vice presidents and associates are not decision makers.
  - The decision maker becomes the lead's main email.
- **Owner detection** (the source is stored in `owner_basis` and shown in the dashboard), in order:
  1. The title on the website.
  2. The one person whose surname the business name or domain contains.
  3. A business name that is a person: "Dr. Ken Mickens DDS", or "Kirk Rathburn" with the
     surname in the domain.
- **Guards added after a live test on 15 Boulder dental sites:**
  - A name containing business words (Dental, Dentistry, Family, Arts, …) is never a person.
  - Honorifics and credentials are removed from names.
  - ALL-CAPS name lines are read.
  - Inbox words are found inside longer addresses (drdanielfrontdesk@).
- **Result of that test:** 6 of 14 companies got a correct owner, none wrong. 11 of 14 main
  emails got a designation.

## 2026-09-27: Overture Maps replaces OpenStreetMap for finding businesses

The public Overpass (OpenStreetMap) servers were down or overloaded most of the day. Even
"5 restaurants in Honolulu" timed out on all four servers, and a Hawaii search found nothing in
10+ minutes. At the user's request the OpenStreetMap business search was **removed entirely**:
the Overpass source, mirrors, tiling, town-by-town search, OSM tag rules, the overpass cache table
and the OSM industry generator.

- **Overture Maps places** are read straight from its public GeoParquet files on S3
  (`pyarrow`, anonymous access, no key or rate limit), projecting only the needed columns and
  filtering by bounding box. Measured live: Boulder, CO (3 industries) 41 s → 1,345 businesses,
  82–88% with a website; all of Hawaii (construction) 88 s → 2,678, 79% with a website, 643 with
  an email already listed. The old OSM search found a few dozen per industry when it worked.
- **Industries map Overture's category tree** (`overture:` lists; 528 categories across the 50
  industries, each in exactly one). A place gets the industry of the **deepest** listed category
  in its path, so `home_service > plumbing` is Construction while other `home_service` places fall
  back to Home services. `_exclude` drops banks, ATMs, hospitals, universities, public schools and
  government. The mapping was built from the categories seen in 80k real places (Honolulu and
  Austin).
- **Quality filters:** skip `operating_status` closed; skip `confidence < 0.5`; keep only places
  inside the place's **border**. Nominatim now returns a simplified polygon
  (`polygon_geojson`, 0.005°) and shapely does the point-in-polygon test. Tiles that don't touch
  the border (sea) are not downloaded.
- **Tiles and cache:** big areas are split into ≤64 tiles of ≥0.5°. Each tile's classified
  businesses (all industries, so other industry picks reuse them) are cached as gzip JSON in
  `data/overture/<release>/` for 30 days. Businesses are saved to the database after every tile,
  so progress survives interruptions, and "continue with what's found" stops between tiles.
- **Kept from OpenStreetMap:** only the place-name search (Nominatim). Overture has no geocoding
  service, and it's one small request per search.
- **Contact data:** Overture's websites, phones and emails are stored as found. Emails still go
  through the junk filter, the 3-contact limit and verification. Brand names feed the chain
  check.

## 2026-09-27: Town-by-town search for big areas, and a place picker

- **States and countries are searched around their cities and towns.** When an area's bounding
  box is wider than `overpass.large_area_degrees` (1°), one query lists its `place=city|town`
  nodes (cities first, then by population, at most 150). Each gets a radius search (12 km for a
  city, 6 km for a town), and results are **saved after every town**, so a run shows businesses
  within a minute and a failure midway keeps what was found. The trade-off is that businesses
  far from any town are missed. The first attempt (a whole-Hawaii query) found nothing in 6+
  minutes.
- **Place picker.** Nominatim results are listed with their type, and the chosen one is stored as
  `LocationSpec.osm` ("R113314") and resolved with `/lookup`, not a new free-text search. Search
  runs on Find/Enter only, because Nominatim's policy forbids autocomplete. CLI: `leadscout places`
  and `-l osm:<id>`. Keys of plain place specs are unchanged, so old runs resume.
- A bare `construction` now means the category; the section needs `group:construction`.
- A new run that reuses a deleted run's number removes that run's old log file.
- Overpass `min_interval_seconds` went from 5 to 2, since town-by-town makes many small queries.

## 2026-09-27: 50 broad categories instead of 208 types

At the user's request the list is now **50 broad categories**, e.g. Construction & contractors =
34 trades (plumbing, roofing, flooring, electrical … plus architects, engineers, surveyors,
inspectors and interior designers). `tools/industry_categories.py` maps every one of the 208
types to exactly one category, and the generator asserts nothing is missing or duplicated. A
category keeps all its types' tags, name patterns and names, so it searches everything its types
did, and every old type word resolves to its category ("roofer" → Construction). Each category
lists its `subtypes`, shown in the CLI and the dashboard.

- **Trade-off:** results are labelled with the category, not the trade, and a single trade can
  no longer be picked from the list. An exact OSM tag (`craft=roofer`) still searches one trade.
- **Resolution** also matches part of a category or subtype name when it points to exactly one
  category ("flooring contractor").
- **Sections** (the 11 former groups) remain as quick picks; `group:<section>` selects a section,
  and a bare `construction` now means the category.

## 2026-09-27: Big areas are split into tiles

A 40-industry search of the whole state of Hawaii timed out at 28, then 14, then 7 industries,
3 minutes each. Splitting industries barely helps, because the weight is the area (Hawaii's
bounding box is mostly ocean).
- A query that is too heavy now splits the **area** first: the Nominatim bounding box (or a
  radius's box) is cut into 4 tiles, recursively, down to ~5 km. Each tile keeps the area filter
  plus a bbox filter, which uses Overpass's spatial index. Ocean tiles return instantly. Only
  then are industries split.
- The per-query server timeout dropped from 180 s to 90 s, so a heavy query is split sooner.
  Chunks are 25 selectors instead of 60.
- Every finished piece is cached, so a resume replays the finished tiles from cache.
- The dashboard suggests fewer industries or a batch list of cities when more than 10
  industries are selected.

## 2026-09-27: Faster name searches, mirror fallback, partial failures

- **Name patterns no longer scan the whole database.** A 208-industry preview timed out even
  in a 2 km radius, because `nwr["name"~"…",i]["office"](around:…)` makes Overpass run the
  regex before the spatial filter. Queries now fetch named objects with the relevant keys inside
  the area into a set (`->.scoped`) and run one combined name regex on that set. Each business is
  labelled on our side by `match_strength`.
- **Heavy queries are split, not fatal.** If the server can't finish a query, the chunk is halved
  and retried down to single industries, and only an industry that still fails is reported
  (its run task is marked failed, so a resume retries it). An *unreachable* server
  (`OverpassUnavailable`) is not split, since smaller queries can't help; it fails fast.
- **Mirror fallback.** overpass-api.de was unreachable (connect timeouts) on 2026-09-27. Queries
  now fail over to public mirrors with the same data (`overpass.fallback_urls`: maps.mail.ru,
  private.coffee, kumi.systems) with no waiting, stick with the mirror that worked, and back off
  only when every server failed a round. The connect timeout is 15 s so a dead server is left
  quickly. The cache key no longer includes the server URL.

## 2026-09-27: New industry list, construction first, no same-work categories

- **The bookkeeping-ranked 200 list was replaced** by the user's 300-category list, with
  contractors first. Categories that are the same business under another name were merged
  (Accounting = CPA firms, Cafés = Coffee shops, Gyms = Fitness centers, three veterinary
  names …). Categories that can't be found on a map were left out (private tutors, online
  education, virtual assistants, ghost kitchens, house flippers …). The gaps were filled with
  other map-findable trades, which gave 300.
- **Then, at the user's request, categories doing the same work were merged:** all law practice
  areas into Law firms; SEO / social / advertising / branding into Marketing agencies; hotels +
  motels; hair salons + barbers; windows + doors + garage doors + glass; and 60+ similar merges
  (see `tools/industry_merges.py`; rebuild with `python tools/gen_industries.py`). **Result: 208 categories.** Merged entries keep every
  tag, name pattern and alias of the ones folded in, so no search is lost and old names resolve.
- **Groups replace tiers.** Each industry has a `group` (11 categories, labels in `_groups`).
  `-i construction` / `group:construction` / the category label select a group; `topN` still
  works. The dashboard's quick-pick buttons are the categories plus Top 25 / Top 50.
- **Most specific label wins.** Results from all chunked queries are collected first, then each
  business is labelled with the strongest match: combined tag > specific tag > name pattern >
  tag of a `broad` catch-all category (general contractors, law firms, restaurants …).
- **Plurals work on stored names too** ("seo agency" finds "SEO agencies").

## 2026-09-27: Non-map industries removed

At the user's request, the 11 ranked industries marked `searchable: false` were deleted from
`industries.yaml` (landlords #25, e-commerce #32, franchise owners #39, and #161–163, #165–169).
Remaining industries keep their original ranks, so there are gaps. The `searchable: false`
mechanism stays for anyone who adds such an entry later. Franchisees are still reachable by
searching with "Include chains & large"; online businesses via Enrich.

## 2026-09-27: At most 3 contacts per company

- `filters.max_contacts_per_company: 3` limits contacts at **collection** (the crawl and
  map-listed emails keep only the best N, which is also data minimisation) and again at
  **export/leads**, because merged companies can end up holding more than N.
- **"Best" at collection** (before verification): named person < generic inbox < department
  inbox, then the company's own domain before freemail. At export the lead score decides, since
  it includes verification status.
- The database was cleared at the user's request before this change; the backups in `data/`
  are from before the clear.

## 2026-09-27: One record per company

The live database (1,610 businesses) had no duplicate domains or phones. It did have
2 acquisitions whose sites redirect to the buyer (kraftcpas.com → bakertilly.com,
mrocpa.us → yeagercpa.com), and 21 names stored several times, mostly chain branches listed
without a website (Liberty Income Tax ×8, H&R Block ×4, Edward Jones ×4).

- **The company key is the registered domain** (`local.jacksonhewitt.com` → `jacksonhewitt.com`),
  using the public-suffix list *including* its private section, plus a `SHARED_HOSTING` list
  (voolt.com, squarespace.com, business.site, …). On those builders each subdomain is a separate
  business. Extra builders go in `shared_hosting_domains`.
- **New rules:** a no-website listing whose email is on a known company's domain joins that
  company; a site that redirects to another known company's site is merged into it (never when
  the redirect goes to a social or non-business host); chain branches with the same
  *distinctive* name become one company with a summed `location_count`.
- **Generic names are never merged on name alone, and never make a chain by repetition.** The
  first cleanup wrongly merged three different "Accounting & Tax Services" firms. The database was
  restored from the automatic backup and the cleanup re-run after adding the generic-name check:
  every word is a trade word from a built-in list plus the industry vocabulary. Two different
  websites under one name are never merged either.
- **Merging is lossless.** Sources, run links and emails move to the kept record, and missing
  details are filled in from the dropped one. Verified on the live data: 535 emails and 1,632
  source records before and after, 0 orphans. Result: 1,610 → 1,583 businesses.
- **Runs dedupe automatically** after discovery and after crawling. `leadscout dedupe` backs up
  the database, then cleans it and re-flags and rescores every run.
- Raymond James, OneMain Financial and Padgett were added to `known_chains.txt`. Padgett's two
  offices keep separate records because they have different websites; both are flagged.

## 2026-09-27: Keeping multinationals out of small-business lists

A user's accountant run included Ernst & Young, Baker Tilly, Fiducial, Pilot.com and a university
cashier's office. None had an OSM brand tag, and each appeared once, so the chain checks passed
them.

- **Wikidata size check.** Each business is searched by name, and a result is accepted only when
  the entity's official website matches the business. The match can be the same registered
  domain, or the same brand label on another country domain *and* the same name (fiducial.com
  vs fiducial.fr). A name match alone is never enough, which rules out films, awards and
  namesakes. Flags: more than 250 employees (the EU SME ceiling), a stock listing,
  multinational/public-company type, or any Wikipedia article (`exclude_wikipedia_notable`,
  because almost no small business has one). Checked live: EY has 300,000 employees and 45
  Wikipedias; Baker Tilly (bakertilly.com) has 1 Wikipedia; Fiducial has 17,500 employees via
  fiducial.fr; small CPAs have no match.
- **It runs after discovery and before crawling**, so large firms aren't crawled at all.
  Results are cached per domain for 90 days, and requests are serial at 0.3 s intervals.
  Wikimedia rejects agents without contact details, so the check runs only when
  `contact_email` is set, and otherwise says so. Three consecutive failures skip the rest of
  the run with a note; `recheck` can finish it later.
- **Website signals** catch large firms that Wikidata misses: an "investor relations" section,
  or 6+ hreflang language/country versions. A bare "Investors" link does *not* count, because
  small realtors and mortgage brokers use it.
- **.edu/.gov-style domains** are non-commercial and flagged.
- **Big accounting networks were added to `known_chains.txt`.** Surnames such as Crowe, Marcum
  and Armanino are listed by domain only, so "Crowe Plumbing" isn't flagged.
- **All of these reuse the chain flag** (`is_chain` + `chain_reason`): excluded by default, and
  shown with a reason. The UI calls it "chains & large companies".

## 2026-09-27: One lead per company

- **Default export and leads mode is `company`.** Colleagues at the same company are one lead,
  because outreach goes to the company once. That lead uses its highest-scoring email, and the
  other addresses go into `other_emails`. `--per-email` or `export.mode: email` restores one
  row per address.
- **Company key.** In order: the business's website domain, else the email's domain when it
  isn't a freemail provider, else the business record. So two map listings of one company
  (acme-north and acme-south, both @acmeplumbing.com) are one lead, and two gmail.com sole
  traders stay two leads.
- **"Only new" works per company in company mode.** Once any email of a company has been
  exported, a colleague's address is not offered as a new lead. Exports record every email in
  a lead row, including the other_emails.

## 2026-09-27: 200 ranked industries for bookkeeping prospecting

- **The ranked list is the industry map.** `config/industries.yaml` now carries all 200 industries
  from the ranking, each with its `rank` and `tier`. The file is generated from the list; the
  script checks that every rank 1–200 is present and that no word is claimed by two industries.
  Existing keys (dentist, cafe, hotel …) were kept so old runs and commands still work.
- **Two splits.** `optician` is now `shop=optician` only, and the new `optometrist` covers
  `healthcare=optometrist`. `barbershop` is split out from `hair_salon`. Guest houses left `hotel`
  and became their own industry (rank 130).
- **Industries that can't be map-searched** are kept in the list with `searchable: false` and a
  note: online sellers, creators, landlords, franchise owners and subscription boxes. Asking for
  one explains why and points to Enrich instead of silently returning nothing.
- **Trades OSM tags poorly** (remodelers, towing, junk removal, pressure washing, HOAs …) use
  name patterns limited to office/craft/shop objects. Expect lower recall and some false matches
  for these. This is noted in the file header.
- **One combined query per place.** Each place gets one union query for all its industries,
  split when it passes 60 selectors (`overpass.max_selectors_per_query`). A tier-1 search in one
  city is now 2 queries instead of 37, which is much kinder to the shared public server. Each
  result is labelled with the highest-ranked matching industry.
- **The max-results cap takes industries in turn**, and within each industry businesses with a
  website or email come first.
- **Checked live on 2026-09-27.** Tiers 1–4 were previewed for Boulder, CO against
  overpass-api.de and every mapping query was accepted. The public server returned several
  429/504 responses under load; the existing backoff recovered each time.

## 2026-09-27: Web dashboard (P2-08 to P2-10, P3-05 UI, P3-07)

- **Streamlit, as planned.** All five screens from section 6.3 exist. A run started from the web
  runs in a background thread with its own database connection (`leadscout/jobs.py`). The
  progress screen refreshes every 2 s, and Pause sets the same stop flag as Ctrl+C in the CLI.
- **One run at a time from the dashboard.** This keeps the public OpenStreetMap servers and our
  per-site limits predictable.
- **Settings saved from the web go to `config/settings.local.yaml`.** They are layered on top of
  `settings.yaml`, so the commented defaults file is never rewritten.
- **Hosted demos.** `contact_email` and `app_password` can come from Streamlit secrets. The
  team sign-in screen stops strangers from using a public link to crawl sites or download leads;
  host-level sharing ("only specific people") can be added on top. The password is compared in
  constant time.
- **Downloads count as exports.** A dashboard download is recorded as an export, so "Only new"
  works the same as in the CLI.

## 2026-09-27: Phase 1 build

**Scope delivered.** All Phase 1 tasks (P1-01 to P1-10) are done. Parts of Phase 2 were pulled in
because the resume design depends on them: the SQLite store (P2-02), the stage-based run manager
with pause/resume (P2-03), cross-run dedupe (P2-04), chain detection (P2-05), scoring (P2-06),
batch locations (P2-07) and "only new" exports (P2-11). From Phase 3, the suppression list (P3-05),
export templates (P3-06) and CSV enrich (P3-08) are done. The Streamlit dashboard, Google Places,
SMTP mailbox checks and catch-all detection are still open.

**Layout.** Configuration lives in `config/` as settings, industries, email rules and known chains,
rather than in a single `config.yaml`. This was already the choice in the initial scaffold, and it
keeps the lists users edit apart from runtime settings. `python cli.py` and the installed
`leadscout` command both work.

**User-Agent must not contain "crawler".** overpass-api.de answers HTTP 406 to such agents
(checked live on 2026-09-27). The default is now `LeadScout/0.1 (self-hosted B2B research tool)`
plus the contact email.

**Robots.txt.** A 401 or 403 on robots.txt disallows the whole site. A 5xx also disallows it,
following RFC 9309 for unreachable robots.txt. A 404 allows everything. If the host itself is
unreachable, the page fetch fails and records the real error.

**Broken TLS certificates.** Many small-business sites have them. The crawler retries once without
certificate verification, because it only reads public pages. If https fails at the connection
level, it also tries the other scheme.

**Email statuses in basic mode.**
- Generic inboxes such as `info@` are **valid**, not risky. The plan's design notes list generic
  inboxes under Risky, but most small businesses publish only `info@`, and a default export of
  valid-only would then be nearly empty. The email type column and the score (+5 generic vs +15
  personal) carry that difference instead.
- Disposable domains are **risky**, as the comment in `email_rules.yaml` says.
- DNS timeouts are **unknown** and are not cached, so a later run retries them.

**No-reply and platform addresses** (`noreply@`, `postmaster@`, Sentry, Wix) are dropped at
extraction and never stored.

**Unrelated domains.** An email is kept only if its domain matches the site, the site's redirect
target, a freemail provider, or shares a clear name token with the site or business. This removes
web-agency and platform addresses. It can miss a business that uses a completely different domain
for mail. We accept that trade.

**Email type uses business context.** An address named after the business
(`boulderdental@gmail.com`, `patient@boulderdentalcenter.com`) is generic, not personal. The
first live test typed `patient@` and `care@` as personal, which inflated scores.

**Require website.** With the default `require_website: true`, a business that has no website but
does have an email in OSM is still kept, because it is a usable lead. Businesses with neither are
stored for dedupe but not added to the run.

**Chains and many locations.**
- The same domain seen more than 500 m away raises `location_count` instead of creating a new
  business. Three or more locations with the same name or domain make it a chain.
- A plain "Locations" menu link does **not** make a chain. The first live test wrongly flagged a
  single-office dentist that way. Only an explicit store locator, or links to three or more
  `/locations/<place>` pages, count.
- Chains are flagged before crawling. With `exclude_chains` on, they are not crawled at all, which
  saves requests.

**Small-team signal.** Follows the plan: a team page showing 50 or fewer people, or no team page
and no careers page.

**First names in exports.** First names are best-effort. They are guessed only from personal
addresses shaped like `first.last@`, or from short single words that don't look like
initial + surname (`anna@` gives Anna, `jsmith@` gives nothing).

## Phase 1 review: open items

- P1-11 still needs a real 500+ lead test on one city and one industry, with a 100-lead manual
  review. A live check of 8 dentists in Boulder, CO worked end to end and surfaced the three
  fixes above.
- Measure crawl throughput on a full run. The target is at least 1,000 sites/hour. The defaults
  are 8 concurrent sites and 2 s per domain; raise `global_concurrency` if needed.
