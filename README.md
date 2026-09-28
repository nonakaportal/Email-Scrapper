# LeadScout

A free, self-hosted lead research tool. Pick an **industry** and a **location**. LeadScout finds
businesses in **Overture Maps** (free open business data from Microsoft, Meta, Amazon and TomTom),
crawls their websites politely for public contact emails, verifies them, scores how likely each
one is a small independent business, and exports clean CSV or Excel files.

Everything runs on your machine and nothing is uploaded. The default setup costs $0.
See [project.md](project.md) for the full plan.

## Setup

Requires Python 3.11+ on Windows, macOS or Linux.

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -e ".[dev]"
```

Then open `config/settings.yaml` (or the dashboard's Settings) and set `contact_email`. The public
place-search and company-size services ask for a way to reach you. It goes into the User-Agent
and is never used for anything else.

## Quick start

```bash
# 1. See how many businesses exist before crawling anything
leadscout preview -i dentist -l "Austin, Texas, USA"

# 2. Run: search -> crawl websites -> verify emails -> score
leadscout run -i dentist -l "Austin, Texas, USA" --max 500

# 3. Look at the results, then export
leadscout leads --run 1
leadscout export --run 1                     # valid emails only (default)
leadscout export --run 1 -s valid -s risky --min-score 60 --format xlsx
```

`python cli.py ...` works as well if you don't want to install the `leadscout` command.

## Web dashboard

```bash
pip install -e ".[dashboard]"      # once
streamlit run app/dashboard.py     # opens http://localhost:8501
```

The dashboard has five screens:

- **New search**: pick industries and a place, a point + radius, or a batch list. Use "Preview
  count" to see how many businesses exist, then "Start". The "Enrich my list" tab takes a CSV of
  websites.
- **Run progress**: live bars for each stage with counts, elapsed and remaining time,
  Pause/Resume, and the run log.
- **Leads**: filter by run, industry, city, status, score and email type. Click a row for all
  emails, where each was found, and the score breakdown. Export to CSV/Excel with a column preset
  for your email tool.
- **Runs**: history, with Resume, Re-run and View leads.
- **Settings**: contact email, crawl speed, scoring weights, the suppression list, and editors for
  industries and known chains. Saved settings go to `config/settings.local.yaml`.

Runs started from the dashboard continue in the background while you switch screens. The CLI
and the dashboard share the same database.

### Sharing it with your team online (Streamlit Community Cloud, free)

1. Push this folder to a GitHub repository (private is fine). `.gitignore` already keeps your
   settings, database, exports and logs off GitHub.
2. Go to https://share.streamlit.io, sign in with that GitHub account and allow access to the
   repository.
3. **Create app**: repository `your-account/your-repo`, branch `master`, main file
   `app/dashboard.py`. Under **Advanced settings** pick Python 3.12, and paste the secrets:
   ```toml
   contact_email = "you@yourdomain.com"   # shown to websites and required by the place search
   app_password = "choose-a-team-password" # everyone must enter it to open the app
   ```
4. **Deploy**. You get a link like `https://your-app.streamlit.app` for your team. It opens on a
   "Team sign-in" screen; send the link and the password separately.
5. Optionally, in the app's **Sharing** settings, choose "Only specific people can view this app"
   and invite your team by email. The password still applies. Without `app_password` the app
   opens with no sign-in, so anyone with the link can run searches and download your leads.

What to expect on the free plan:
- **Data is wiped** when the app restarts or wakes from sleep. Use **Settings → Backup →
  Download backup** after each run, and **Restore** it after a restart. Export leads you need.
- **Limited memory.** Keep runs small (one city, one or two industries, Max businesses 50–300).
  A very large area can crash the app, and then the data is wiped.
- **No mailbox check or email guessing** (the host blocks port 25). Everything else works.
- One search runs at a time, shared by everyone. Others can browse and export meanwhile.
- The app sleeps after some days without visitors; the next visitor wakes it (about a minute).

## Commands

| Command | What it does |
|---|---|
| `leadscout industries` | List the 50 industries |
| `leadscout preview -i WORD -l PLACE` | Count businesses on the map without crawling |
| `leadscout run -i WORD -l PLACE` | Full run. **Ctrl+C pauses** and nothing is lost |
| `leadscout resume RUN_ID` | Continue a paused or failed run |
| `leadscout runs` | Past runs with counts and status |
| `leadscout enrich FILE.csv` | Crawl and verify your own list of websites |
| `leadscout mailboxes RUN_ID` | Ask the mail servers whether a run's emails really exist |
| `leadscout leads [filters]` | Show matching leads in the terminal |
| `leadscout export [filters]` | Write CSV/Excel to `exports/` |
| `leadscout suppress add/remove/list/import` | Do-not-contact list, applied to every export |

**Locations**

In the dashboard, type a place and press **Find** to see every matching place with its type (city,
county, state, country), then pick the exact one (Springfield, Illinois rather than Springfield,
Missouri). In the terminal: `leadscout places "Springfield"`, then `-l place:R123456` from the
list. Only businesses inside the chosen place's border are kept. States and countries are
downloaded in parts (tiles), saving businesses as each part finishes; parts that are only sea are
skipped.


```bash
leadscout run -i plumber -l "Leeds, UK"                              # place name
leadscout run -i "hair salon" --point "30.2672,-97.7431" --radius-km 15  # point + radius
leadscout run -i cafe -l "Portland, Oregon" --radius-km 5             # around a place
leadscout run -i lawyer -l "Austin, TX" -l "Dallas, TX"               # several places
leadscout run -i accountant --locations-file cities.txt               # batch, one per line
```

**Industries: 50 broad categories.** `config/industries.yaml` has 50 industries, construction
first. Each covers all of its related businesses. For example, **Construction & contractors**
covers general contractors, builders, plumbers, roofers, electricians, HVAC, flooring, painters,
masons, architects, engineers, surveyors, inspectors, interior designers and more. In the file,
each industry lists the Overture Maps categories it covers (`overture:`); a business belongs to the
industry of the most specific category it has. Banks, ATMs, hospitals, universities and government
offices are never leads (`_exclude:`). You can pass:

- an industry or any word for it: `construction`, `plumber`, `roofers`, `seo agency`, `vet`
- a whole section: `-i group:construction`, `-i group:healthcare` …, or the top N: `-i top10`

Several industries are searched together in one download per place. With `--max`,
businesses are taken from each industry in turn, so the cap isn't filled by the first
industry.

```bash
leadscout preview -i construction -l "Denver, Colorado, USA"     # what exists
leadscout run -i construction -i home_services -l "Denver, Colorado, USA" --max 500
```

**Export filters.** `--status/-s` (valid, risky, invalid, unknown), `--min-score`, `--industry`,
`--city`, `--type` (personal, generic, role), `--run`, `--only-new` (skip companies
exported before), `--per-email` (one row per email instead of per company), `--include-chains`, `--format csv|xlsx`, and
`--template default|instantly|smartlead|lemlist|hubspot`.

**At most 3 contacts per company** (`filters.max_contacts_per_company`, or Settings → Crawling).
The best are kept when collecting and again when exporting: named people first, then info@-style
inboxes, then department inboxes (sales@, billing@), with the company's own domain ahead of
Gmail and similar.

**One lead per company.** When several people at the same company have an email, such as
anna@, info@ and billing@ at one dental practice, they count as **one lead**. The row holds the
best email (the decision maker first, then personal and valid), and the colleagues go in the
`other_contacts` column with their designations, for example
`Mark Chen (Office Manager) <mark@x.com>; Billing / Accounts <billing@x.com>`. The same company
appearing twice on the map (same website, or the same company email domain) is also one lead.
Two unrelated businesses that both use gmail.com are never merged. Use `--per-email` (CLI) or turn
off "One lead per company" (dashboard) to get one row per address.

**Names and designations.** Each email gets the name and job title of its person, as the
business's own website states them (team and about pages, "Mark Chen - Office Manager" lines,
schema.org data, "Dr. Craig Larson" in a bio). Shared inboxes get a role from the address
(`sales@` → Sales, `frontdesk@` → Front desk / Office). Nothing is guessed: when the website
doesn't say, the designation stays empty. Owners, founders, CEOs, presidents and managing
partners are marked as **decision makers** (⭐ in the dashboard) and become the main contact.
The **owner** columns name the business's owner even when their own email isn't published: the
person the website calls owner/founder, or the person the business is named after ("Larson
Dental" → Dr. Craig Larson, "Kirk Rathburn" at rathburndental.com). The dashboard shows why.
Export columns: `contact_name`, `designation`, `decision_maker`, `owner_name`, `owner_title`,
`other_contacts` (mapped to Job Title in the Instantly, Smartlead, lemlist and HubSpot templates).

**People and seniority.** LeadScout lists the people each company names on its website
(`key_people`, most senior first, with their email when published). It reads team cards,
"Name - Title" lines, structured data, and sentences like "founded in 1998 by John Smith" or "our
president, Tom Wu". It always visits the best leadership / team / about page. Titles get a level:
**Owner / CEO** (owner, founder, CEO, president, managing partner), **Executive / Director**
(director, VP, chief officers, general manager, partner), **Manager**, or **Staff**. Owner / CEO and
Executive / Director count as decision makers (⭐).

**Guessed emails for decision makers (full verification only).** When the website names an owner,
CEO, director or VP but doesn't publish their email, LeadScout builds the likely address from the
company's own format (`rebecca@` → `chris@`, `anna.lopez@` → `chris.martinez@`). It asks the mail
server whether that address exists. It keeps the address **only when the server confirms it**; catch-all
servers confirm nothing, so nothing is kept for them. Written emails always come first, guessed
ones after. In Excel exports and on the Leads page, written emails are **green** and guessed ones
**yellow**; in CSV, `email_found_in` says "guessed, confirmed by the mail server". Needs port 25,
and servers such as Microsoft 365 refuse home internet addresses listed by Spamhaus, so it works
best from a server. `email.guess_emails: false` turns it off.

**Best email to contact.** Each lead's main email is the best one to reach the business, in this
order:
1. Written somewhere (the website, a listing) before guessed-and-confirmed emails.
2. The most senior person: owner / CEO, then director / VP, then manager.
2. A deliverable address: valid first, then unknown, then risky (catch-all).
3. Who reads it: a named person, another personal address, the main inbox (info@, office@),
   department inboxes (sales@, billing@), and social-media inboxes (facebook@) last.
4. Seen on the business's own website, rather than only in the map data.
5. Confirmed by the mail server.
6. On the company's own domain, rather than Gmail and similar.

The same order decides which 3 emails a company keeps. A better email found later replaces a
weaker one, unless the weaker one was already exported.

**Only businesses still operating.** Each business gets an activity status, with the reason shown:

| Status | When |
|---|---|
| Active | website online, plus a recent sign: dated content on the site, a map listing updated in the last 18 months, or a mailbox confirmed by its mail server |
| Likely active | website online but nothing dated, or no website but a recent map listing |
| Unclear | website unreachable, no website and an old listing, or the website now goes to another company's site (sold, merged or taken over) |
| Likely closed | the homepage says it closed, the domain no longer exists (confirmed by DNS), or it is parked, for sale or its hosting is suspended |

Exports and the Leads page show only **Active** and **Likely active** businesses by default
(`export.activity`; `--activity all` or the "Business activity" filter to change). When a
website now goes to an unrelated company, nothing on that site is used: no emails, people or
phones.

**Latest email first.** Every email has a last-seen date: the day it was found on the live
website, or the date the map's data provider (Foursquare, Meta ...) last updated the listing.
`email_found_in` says which, for example `map listing (Foursquare, 2025-06-25)`. Among equally
good emails, the most recent one becomes the main contact.

**Website and social links** are in every export: `website`, `facebook`, `instagram`,
`linkedin`, `x_twitter`, `youtube` and `tiktok`. They come from the business's website and from
the map data, which often has a Facebook page even when the website has no links.

**Enrich your own list.** Give it a CSV with a `website` column (also recognised: `url`, `domain`).
The optional `name`, `phone`, `city` and `country` columns are used when present.

## How it works

```
place search (Nominatim: name -> border)  ->  Overture Maps (businesses in that border)
  -> dedupe into SQLite -> flag chains -> crawl websites -> company-size check
  -> verify emails -> score -> export
```

- **Find businesses.** LeadScout reads Overture's open data files directly from Amazon's cloud
  (no API key, no rate limit), downloading only the needed columns for the place's area, in
  tiles for big areas. It keeps businesses that are open (not "permanently closed"), that
  Overture is reasonably sure about (`overture.min_confidence`), that are inside the place's
  border, and that belong to a chosen industry. Websites, phones and emails from Overture are
  saved straight away. Downloaded parts are kept in `data/overture/` for 30 days, and place
  lookups are cached for 90 days. Example: construction in Boulder, CO took 41 s (483
  businesses, 82% with a website); all of Hawaii took 88 s (2,678).
- **One record per company.** A listing is merged into an existing company when it has:
  - the same main website: `local.jacksonhewitt.com` = `jacksonhewitt.com`, but separate
    businesses on site builders (`joes.wixsite.com`, `*.squarespace.com`, …) stay apart
  - the same phone number, or the same name within 100 m
  - no website, but an email on a known company's domain
  - a website that redirects to a known company's site (e.g. after an acquisition)
  - the same distinctive name as a chain's other branches

  Generic trade names such as "Accounting & Tax Services" are never merged on name alone. The
  same company found far apart counts as extra locations. Runs apply all this automatically;
  `leadscout dedupe` cleans up an existing database and saves a backup copy first.
- **Crawl.** The crawler fetches the homepage, then contact, impressum, about and team pages, up
  to 5 pages per site. It checks robots.txt first, waits 2 s between requests to the same domain,
  retries with backoff, and caps page size.
- **Extract.** Emails come from `mailto:` links, visible text, HTML entities, JSON-escaped text,
  Cloudflare-protected addresses and `name [at] domain [dot] com` obfuscation. Placeholder,
  platform and file-name addresses are dropped, as are addresses whose domain has nothing to do
  with the business (for example the web agency that built the site). The crawler also records
  phones, social profiles, the site platform, the copyright year and a team-size hint.
- **Verify.** Each email gets a syntax check, a disposable-domain check, and a mail server
  lookup (MX, falling back to A records). The result is **valid**, **risky**, **invalid** or
  **unknown**, with a reason.
- **Mailbox check** (full verification, optional). Choose "Full" under Verification on New
  search, use `--verify full`, or press "Check mailboxes" on a finished run (`leadscout mailboxes
  RUN_ID`). LeadScout asks each company's mail server whether the address exists, then hangs up.
  No email is sent. The server's answer:
  - the mailbox exists: **valid**, confirmed
  - no such mailbox (for example someone who left): **invalid**
  - the server accepts any address (catch-all): **risky**
  - no answer, or it refused to talk to us: the basic result stays, marked "not confirmed"

  Gmail, Outlook and Yahoo addresses are not asked, because they don't give real answers. Checks
  are slow on purpose: about 6 connections a minute per mail provider, one connection per company
  domain. Answers are cached for 14 days.

  It needs outbound **port 25**. Most home internet providers and cloud hosts block it, and then
  the step skips itself. Many servers (Microsoft 365 in particular) also refuse home internet
  addresses listed by Spamhaus, so from a home connection some checks stay "not confirmed". For
  the best results, run it from a server that allows port 25, and set `email.smtp_from_domain` to a
  domain you own. Without it, the standard empty sender (`MAIL FROM:<>`) is used.
- **Chains and large companies** are excluded by default, because the target is small
  independent businesses. A business is flagged if any of these apply:
  - a brand in Overture's data, or a name/domain in `config/known_chains.txt` (big accounting
    networks are included)
  - the same name or website at 3+ places, or a store locator on its site
  - **Wikidata** (free, public) knows the company, confirmed by its website, and shows it is
    large: more than 250 employees, stock-listed, multinational, or has a Wikipedia article.
    This needs `contact_email`, as Wikimedia requires.
  - its website has an investor relations section, or comes in 6+ language/country versions
  - its website is a .edu/.gov (or similar) domain

  Each lead shows why it was flagged. Thresholds are under `size_check:` in settings.
  `leadscout recheck RUN_ID` (or "Re-check size" on the Runs page) applies the checks to an
  earlier run without crawling again.
- **Score** (0–100). Scoring uses email status and type, independence, single location, phone,
  recent website activity, social presence and team size. The weights are under `scoring:` in
  `config/settings.yaml`.

Each stage saves results as it goes, so an interrupted run resumes where it stopped. Logs are
written to `logs/run_<id>.log`.

## Configuration

| File | Purpose |
|---|---|
| `config/settings.yaml` | User-Agent and contact email, rate limits, crawler, filters, scoring weights, export defaults |
| `config/industries.yaml` | The 50 industries: Overture categories they cover, search words |
| `config/email_rules.yaml` | Generic/role inboxes, freemail, disposable and junk lists |
| `config/known_chains.txt` | Chain names and domains to flag |

## Limits and good practice

- Revenue and MRR are not public for private businesses. The score uses proxy signals instead.
- Results depend on Overture's coverage, which is strongest in North America, Europe and
  Australia and refreshed monthly (a brand-new business can be a month late). Run `preview` first.
- Some sites block automated visitors (HTTP 403) or show only a contact form. LeadScout respects
  those blocks and records the reason.
- A run stopped by a connection problem keeps everything found so far; resume it later.
- **Compliance.** Check the rules where your recipients are (CAN-SPAM, GDPR/PECR, CASL, DPDP,
  Spam Act), and honour opt-outs through `leadscout suppress`. Business data © Overture Maps
  Foundation (CDLA-Permissive-2.0); place search © OpenStreetMap contributors (Nominatim). Credit
  them if you publish the data. This is not legal advice.

## Development

```bash
pytest            # unit, fixture and end-to-end tests (all network calls are mocked)
```

## Not built yet

Google Places and scheduled runs. See the Phase 2–4 backlog in [project.md](project.md).
