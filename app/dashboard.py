"""LeadScout web dashboard.

Run locally:   streamlit run app/dashboard.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import yaml  # noqa: E402

from leadscout.config import load_config, save_local_settings  # noqa: E402
from leadscout.db.store import Store, loads  # noqa: E402
from leadscout.export import (  # noqa: E402
    DEFAULT_COLUMNS,
    TEMPLATES,
    ExportFilters,
    export_filename,
    record_export,
    render_export,
    select_leads,
)
from leadscout.export.exporter import (  # noqa: E402
    EMAIL_GREEN,
    EMAIL_GREEN_TEXT,
    EMAIL_YELLOW,
    EMAIL_YELLOW_TEXT,
)
from leadscout.industries import (  # noqa: E402
    NotOnMap,
    UnknownIndustry,
    load_groups,
    load_rules,
    ranked,
)
from leadscout.jobs import JobRegistry  # noqa: E402
from leadscout.location import LocationError, LocationResolver, LocationSpec  # noqa: E402
from leadscout.run_manager import RunManager, run_stats  # noqa: E402
from leadscout.sources.csv_import import CSVImportError  # noqa: E402

st.set_page_config(page_title="LeadScout", page_icon=":material/travel_explore:", layout="wide")

STATUS_BADGE = {"valid": "🟢 Valid", "risky": "🟠 Risky", "invalid": "🔴 Invalid",
                "unknown": "⚪ Unknown", "suppressed": "⚫ Suppressed"}
RUN_BADGE = {"done": "🟢 Done", "running": "🔵 Running", "paused": "🟠 Paused",
             "failed": "🔴 Failed", "pending": "⚪ Pending", "interrupted": "🟠 Interrupted"}
STAGE_TITLES = [("discover", "1. Finding businesses"), ("crawl", "2. Crawled websites"),
                ("size", "3. Company size check"), ("verify", "4. Verified emails"),
                ("mailbox", "4b. Mailbox check"), ("score", "5. Scored")]
VERIFY_MODES = {"basic": "Basic (syntax + mail domain)", "full": "Full (also ask the mail server)"}
EMAIL_TYPES = ["personal", "generic", "role"]
# "running" without a live job means the server stopped mid-run.
RESUMABLE = ("paused", "failed", "pending", "running")
STATUSES = ["valid", "risky", "invalid", "unknown"]
ACTIVITY = ["active", "likely active", "unclear", "likely closed"]
ACTIVITY_BADGE = {"active": "🟢 Active", "likely active": "🟢 Likely active", "unclear": "⚪ Unclear",
                  "likely closed": "🔴 Likely closed"}


# --------------------------------------------------------------- plumbing

def _secret(name: str) -> str | None:
    try:
        return st.secrets.get(name)
    except Exception:  # noqa: BLE001 - no secrets file
        return None


if _secret("contact_email") and not os.environ.get("LEADSCOUT_CONTACT_EMAIL"):
    os.environ["LEADSCOUT_CONTACT_EMAIL"] = _secret("contact_email")


@st.cache_resource
def registry() -> JobRegistry:
    return JobRegistry()


cfg = load_config()
store = Store(cfg.db_path)


def manager() -> RunManager:
    return RunManager(cfg, store)


def password_gate() -> None:
    password = os.environ.get("LEADSCOUT_APP_PASSWORD") or _secret("app_password")
    if not password or st.session_state.get("authed"):
        return
    st.title("LeadScout")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Enter", type="primary"):
            if entered == password:
                st.session_state["authed"] = True
                st.rerun()
            st.error("Wrong password.")
    st.stop()


def active_job():
    jobs = registry().active()
    return jobs[0] if jobs else None


def industries_text(run: dict, limit: int = 3) -> str:
    """'roofer, hvac' or, for long lists, '40 industries (general contractor, remodeler, roofer…)'."""
    names = [i.replace("_", " ") for i in run["industries"]]
    if not names:
        return "your list"
    if len(names) <= limit + 1:
        return ", ".join(names)
    return f"{len(names)} industries ({', '.join(names[:limit])}…)"


def run_label(run: dict) -> str:
    return (f"#{run['id']} · {industries_text(run)} · {run['location_input'][:40]} · "
            f"{RUN_BADGE.get(run['status'], run['status'])}")


@st.dialog("Discard run")
def discard_dialog(run_id: int) -> None:
    run = store.get_run(run_id)
    if run is None:
        st.write("This run no longer exists.")
        return
    counts = store.discard_preview(run_id)
    st.write(f"**Run #{run_id}**: {industries_text(run)} · {run['location_input']}")
    st.write("This permanently removes:")
    st.markdown(f"- the run and its progress\n"
                f"- **{counts['businesses']:,} businesses** and **{counts['emails']:,} emails** "
                "that only this run found")
    if counts["shared"]:
        st.caption(f"{counts['shared']:,} businesses also found by other runs are kept.")
    st.caption("Saved map lookups are kept, so searching the same place again stays fast.")
    c1, c2 = st.columns(2)
    if c1.button("Discard", type="primary", icon=":material/delete:", width="stretch"):
        store.discard_run(run_id)
        st.session_state.pop("run_id", None)
        if st.session_state.get("leads_run") == run_id:
            st.session_state["leads_run"] = None
        st.toast(f"Run #{run_id} discarded.")
        st.rerun()
    if c2.button("Cancel", width="stretch"):
        st.rerun()


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "–"


def start_run(run_id: int, action: str = "execute") -> None:
    registry().start(run_id, action)
    st.session_state["run_id"] = run_id
    st.switch_page(PAGES["progress"])


def footer() -> None:
    st.divider()
    st.caption("Business data © Overture Maps Foundation (CDLA-Permissive-2.0); place search © "
               "OpenStreetMap contributors (Nominatim). LeadScout respects robots.txt and "
               "rate limits. Check local email-marketing rules (CAN-SPAM, GDPR, CASL…) before outreach.")


# ------------------------------------------------------------ new search

def page_new_search() -> None:
    st.title("New search")
    if not cfg.contact_email:
        st.warning("Set your contact email in **Settings** first. The public place-search and "
                   "company-size services "
                   "ask for a way to reach you.", icon=":material/mail:")
    tab_search, tab_enrich = st.tabs(["Search the map", "Enrich my list"])
    with tab_search:
        search_form()
    with tab_enrich:
        enrich_form()


def industry_label(rules, key: str) -> str:
    rule = rules.get(key)
    if rule is None:
        return key
    return f"#{rule.rank} {rule.label}" if rule.rank else rule.label


def search_form() -> None:
    rules = load_rules(cfg.industries)
    group_labels = load_groups(cfg.industries)

    st.subheader("Industry")
    st.caption("Click one or more industries. Each one covers all of its related businesses.")
    sections: dict[str, list[str]] = {}
    for rule in ranked(rules):
        if rule.on_map:
            sections.setdefault(rule.group or "other", []).append(rule.key)
    industries: list[str] = []
    for i, (group, keys) in enumerate(sections.items()):
        state_key = f"industry_pills_{group}"
        if state_key in st.session_state:   # drop names from an older list (page left open across an update)
            st.session_state[state_key] = [k for k in st.session_state[state_key] or [] if k in keys]
        picked = st.pills(group_labels.get(group, group.replace("_", " ").title()), keys,
                          selection_mode="multi", key=state_key,
                          default=keys[:1] if i == 0 and state_key not in st.session_state else None,
                          format_func=lambda k: rules[k].label)
        industries += list(picked or [])
    if len(industries) > 10:
        st.info(f"**{len(industries)} industries selected.** Max businesses is shared between them. "
                "Many industries across a whole state or country is slow on the free map servers: "
                "for faster results pick fewer industries, or use **Batch list** with the cities you "
                "want.", icon=":material/lightbulb:")

    st.subheader("Location")
    mode = st.segmented_control("Location type", ["Place name", "Point + radius", "Batch list"],
                                default="Place name", label_visibility="collapsed")
    specs: list[LocationSpec] = []
    if mode == "Point + radius":
        c1, c2 = st.columns([3, 1])
        where = c1.text_input("Address or lat,lng", placeholder="30.2672,-97.7431 or 1 Congress Ave, Austin")
        radius = c2.number_input("Radius (km)", 1.0, 100.0, 10.0, step=1.0)
        if where.strip():
            specs = [LocationSpec("radius", where.strip(), radius)]
    elif mode == "Batch list":
        text = st.text_area("One place per line", placeholder="Austin, Texas, USA\nDallas, Texas, USA",
                            height=120)
        specs = [LocationSpec("place", ln.strip()) for ln in text.splitlines() if ln.strip()]
    else:
        specs = place_picker()

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Sources")
        st.checkbox("Overture Maps (free)", value=True, disabled=True,
                    help="Open business data from the Overture Maps Foundation (Microsoft, Meta, Amazon, TomTom).")
        st.checkbox("Google Places (capped)", value=False, disabled=True, help="Coming in a later phase.")
        st.subheader("Verification")
        verification = verification_choice("search")
    with c2:
        st.subheader("Filters")
        f = cfg.get("filters")
        require_website = st.checkbox("Must have website", value=f["require_website"],
                                      help="Businesses without a website are kept only if the map lists an email.")
        exclude_chains = st.checkbox("Exclude chains", value=f["exclude_chains"])
        max_results = st.number_input("Max businesses", 10, 20000, int(f["max_results"]), step=50)

    ready = bool(industries and specs)
    b1, b2, _ = st.columns([1, 1, 3])
    preview_clicked = b1.button("Preview count", disabled=not ready, width="stretch",
                                icon=":material/visibility:")
    start_clicked = b2.button("Start", type="primary", disabled=not ready, width="stretch",
                              icon=":material/play_arrow:")
    st.caption("Preview runs only the map search, so you can see how many businesses exist "
               "before crawling their websites.")

    if preview_clicked:
        m = manager()
        try:
            with st.spinner("Downloading businesses from Overture Maps…"):
                st.session_state["preview"] = m.preview(industries, specs)
        except (UnknownIndustry, NotOnMap) as exc:
            st.error(str(exc))
        finally:
            m.close()
    if st.session_state.get("preview"):
        show_preview(st.session_state["preview"])

    if start_clicked:
        job = active_job()
        if job:
            st.error(f"Run #{job.run_id} is still running. Pause it or wait for it to finish.")
            return
        m = manager()
        try:
            run_id = m.create_search_run(industries, specs, max_results=int(max_results),
                                         require_website=require_website, exclude_chains=exclude_chains,
                                         verification=verification)
        except (UnknownIndustry, NotOnMap, ValueError) as exc:
            st.error(str(exc))
            return
        finally:
            m.close()
        st.session_state.pop("preview", None)
        start_run(run_id)


@st.cache_data(ttl=600, show_spinner=False)
def port25_status() -> bool:
    from leadscout.verify.smtp_check import port25_open
    return port25_open()


def verification_choice(key: str) -> str:
    default = "full" if cfg.get("email.smtp_check", False) else "basic"
    mode = st.radio("Verification", list(VERIFY_MODES), index=list(VERIFY_MODES).index(default),
                    format_func=VERIFY_MODES.get, key=f"verify_{key}", label_visibility="collapsed",
                    help="Full also asks each company's mail server whether the mailbox exists, then hangs "
                         "up (no email is sent). Catches people who left and typos; catch-all servers "
                         "become Risky. Gmail/Outlook/Yahoo addresses are not asked. Slower: about "
                         "6 checks a minute per mail provider.")
    if mode == "full" and not port25_status():
        st.warning("Port 25 is blocked on this network, so mail servers can't be asked. The run will "
                   "use the basic check. Most home internet and cloud hosts block it.",
                   icon=":material/block:")
    return mode


def place_picker() -> list[LocationSpec]:
    """Type a place, press Find, pick the exact match (city, county, state, country)."""
    c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
    where = c1.text_input("Place", placeholder="Austin, Texas, USA", key="place_query",
                          help="A city, county, state or country. Press Find to pick the exact one.")
    find = c2.button("Find", icon=":material/search:", width="stretch", disabled=not where.strip())
    where = where.strip()
    if find:
        resolver = LocationResolver(cfg, store)
        try:
            st.session_state["place_results"] = (where, resolver.suggest(where))
        except LocationError as exc:
            st.error(str(exc))
        finally:
            resolver.close()
    query, results = st.session_state.get("place_results", ("", []))
    if not where:
        return []
    if query != where or not results:
        if query == where and not results:
            st.warning(f"No place called '{where}' found. Check the spelling or add the country.")
        else:
            st.caption("Press **Find** to see the matching places and pick the exact one, "
                       "or just continue and the best match is used.")
        return [LocationSpec("place", where)]
    labels = {r["osm"]: f"{r['label']}  ·  {r['kind']}" for r in results}
    choice = st.radio("Which place?", list(labels), format_func=labels.get, key=f"place_pick_{where}")
    picked = next(r for r in results if r["osm"] == choice)
    if not picked["area"]:
        st.caption("This place has no border on the map, so LeadScout searches around its centre.")
    return [LocationSpec("place", picked["label"], osm=picked["osm"])]


def show_preview(rows: list[dict]) -> None:
    table, total, with_site = [], 0, 0
    for r in rows:
        if "error" in r:
            st.error(f"{r['location']}: {r['error']}")
            continue
        total += r["businesses"]
        with_site += r["with_website"]
        table.append({"Location": r["location"], "Rank": r.get("rank"), "Industry": r["industry"],
                      "Businesses": r["businesses"], "With website": r["with_website"],
                      "Email on map": r["with_email"], "Chains (brand tag)": r["branded"]})
    for note in dict.fromkeys(r["note"] for r in rows if r.get("note")):
        st.info(note)
    if table:
        df = pd.DataFrame(table)
        m = st.columns(3)
        m[0].metric("Businesses on the map", f"{total:,}")
        m[1].metric("With a website", f"{with_site:,}", pct(with_site, total), delta_color="off")
        m[2].metric("Industries with results", f"{(df['Businesses'] > 0).sum()} of {len(df)}")
        empty = int((df["Businesses"] == 0).sum())
        if empty and len(df) > 5 and not st.toggle(f"Show {empty} industries with no results"):
            df = df[df["Businesses"] > 0]
        st.dataframe(df.sort_values(["Location", "Businesses"], ascending=[True, False]),
                     hide_index=True, width="stretch")
        resolved = {r["location"]: r["resolved"] for r in rows if r.get("resolved")}
        st.caption(" · ".join(f"{k} → {v}" for k, v in resolved.items()))
    if rows and total == 0 and table:
        st.warning("No businesses found. Try a nearby larger city, a radius search, or another industry word.")
    elif total and with_site / total < 0.3:
        st.warning(f"Only {pct(with_site, total)} of these businesses list a website, "
                   "so few emails can be found.")


def enrich_form() -> None:
    st.write("Upload a CSV with a **website** column (also: url, domain). Optional columns: "
             "name, phone, city, country, email.")
    up = st.file_uploader("CSV file", type=["csv"])
    rules = load_rules(cfg.industries)
    industry = st.selectbox("Industry label (optional)", [""] + list(rules),
                            format_func=lambda k: rules[k].label if k else "—")
    verification = verification_choice("enrich")
    if st.button("Start enrichment", type="primary", disabled=up is None, icon=":material/play_arrow:"):
        if active_job():
            st.error("Another run is still running. Pause it or wait for it to finish.")
            return
        upload_dir = cfg.db_path.parent / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        path = upload_dir / f"{int(time.time())}-{Path(up.name).name}"
        path.write_bytes(up.getvalue())
        m = manager()
        try:
            run_id = m.create_enrich_run(path, industry or None, verification=verification)
        except (CSVImportError, UnknownIndustry, ValueError) as exc:
            st.error(str(exc))
            return
        finally:
            m.close()
        start_run(run_id)


# --------------------------------------------------------------- progress

def page_progress() -> None:
    runs = store.list_runs(50)
    if not runs:
        st.title("Run progress")
        st.info("No runs yet. Start one on **New search**.")
        return
    ids = [r["id"] for r in runs]
    wanted = st.session_state.get("run_id")
    index = ids.index(wanted) if wanted in ids else 0
    by_id = {r["id"]: r for r in runs}
    run_id = st.selectbox("Run", ids, index=index, format_func=lambda i: run_label(by_id[i]))
    st.session_state["run_id"] = run_id
    job = registry().get(run_id)
    if job and job.active:
        live_panel(run_id)
    else:
        render_panel(run_id)


@st.fragment(run_every="2s")
def live_panel(run_id: int) -> None:
    job = registry().get(run_id)
    render_panel(run_id)
    if not job or not job.active:
        st.rerun()


def render_panel(run_id: int) -> None:
    run = store.get_run(run_id)
    job = registry().get(run_id)
    active = bool(job and job.active)
    stats = run_stats(store, run_id)

    head, btns = st.columns([4, 2])
    head.subheader(f"Run #{run_id} · {industries_text(run)} · {run['location_input']}")
    status = "running" if active else run["status"]
    if not active and status == "running":
        status = "interrupted"
    head.caption(RUN_BADGE.get(status, status) + (f" · stage: {run['stage']}" if run.get("stage") else ""))
    with btns:
        c1, c2 = st.columns(2)
        if active:
            if c1.button("Stopping…" if job.stop_requested else "Pause", icon=":material/pause:",
                         width="stretch", disabled=job.stop_requested, key=f"pause{run_id}",
                         help="Stops within a few seconds; everything found so far is kept. "
                              "Resume continues where it stopped."):
                job.stop()
                st.toast("Pausing: finishing the websites in progress (a few seconds)…")
        elif run["status"] in RESUMABLE:
            if c1.button("Resume", type="primary", icon=":material/play_arrow:", width="stretch",
                         key=f"resume{run_id}"):
                if active_job():
                    st.error("Another run is still running.")
                else:
                    start_run(run_id)
        if c2.button("View leads", icon=":material/table:", width="stretch", key=f"leads{run_id}"):
            st.session_state["leads_run"] = run_id
            st.switch_page(PAGES["leads"])
        if active and job.progress.snapshot()[2] == "discover" and not job.stop_requested:
            if st.button("Continue with what's found", icon=":material/fast_forward:", width="stretch",
                         type="primary", key=f"skip{run_id}",
                         help="Stop searching more places and go straight to the websites of the "
                              "businesses found so far."):
                job.skip_discovery()
                st.toast("Finishing the current place, then moving on to the websites…")
        if st.button("Discard run", icon=":material/delete:", width="stretch", key=f"discard{run_id}",
                     disabled=active, help="Pause the run first." if active else
                     "Delete this run and the businesses only it found."):
            discard_dialog(run_id)

    snapshot, notes, _ = job.progress.snapshot() if job else ({}, [], None)
    db_bars = {
        "discover": (len(store.run_tasks(run_id)) - len(store.run_tasks(run_id, "pending")),
                     len(store.run_tasks(run_id))),
        "size": (len(store.run_businesses(run_id, "b.domain IS NOT NULL AND b.org_info IS NOT NULL AND "
                                                 "EXISTS (SELECT 1 FROM emails e WHERE e.business_id = b.id)")),
                 len(store.run_businesses(run_id, "b.domain IS NOT NULL AND "
                                                 "EXISTS (SELECT 1 FROM emails e WHERE e.business_id = b.id)"))),
        "crawl": (stats["crawled"], stats["with_website"]),
        "verify": (stats["verified"], stats["emails"]),
        "mailbox": (stats["mailbox_checked"], stats["mailbox_checked"] + stats["mailbox_todo"]),
        "score": (stats["scored"], stats["businesses"]),
    }
    for key, title in STAGE_TITLES:
        if run["kind"] == "enrich" and key == "discover":
            continue
        if key == "mailbox" and not (stats["mailbox_checked"] or "mailbox" in snapshot
                                     or (run["settings"] or {}).get("verification") == "full"):
            continue
        if active and key in snapshot:
            done, total = snapshot[key]["done"], snapshot[key]["total"]
        else:
            done, total = db_bars[key]
        frac = min(1.0, done / total) if total else (1.0 if run["status"] == "done" else 0.0)
        st.progress(frac, text=f"**{title}** — {done:,} / {total:,}")

    m = st.columns(4)
    m[0].metric("Businesses", f"{stats['businesses']:,}",
                help=f"{stats['found']:,} found on the map" if stats["found"] else None)
    m[1].metric("With an email", f"{stats['with_email']:,}", pct(stats["with_email"], stats["businesses"]),
                delta_color="off")
    m[2].metric("Emails found", f"{stats['emails']:,}")
    m[3].metric("Valid emails", f"{stats['valid']:,}", pct(stats["valid"], stats["emails"]), delta_color="off")
    m = st.columns(4)
    m[0].metric("Risky / Invalid / Unknown", f"{stats['risky']} / {stats['invalid']} / {stats['unknown']}")
    m[1].metric("Chains & large companies", f"{stats['chains']:,}",
                help="Excluded from crawling and exports: chains, franchises, multinationals, "
                     "well-known companies and non-commercial sites. See each lead's detail panel for why.")
    m[2].metric("Errors", f"{stats['errors']:,}", help="Sites that blocked us, were down, or had no reachable homepage.")
    m[0].caption(f"🟢 {stats.get('active_businesses', 0):,} active · 🔴 {stats.get('closed_businesses', 0):,} likely closed")
    if active:
        elapsed = time.time() - job.started_at
        remaining = None
        crawl = snapshot.get("crawl")
        if crawl:
            gained = crawl["done"] - crawl["start_done"]
            spent = time.time() - crawl["started"]
            if gained and spent:
                remaining = (crawl["total"] - crawl["done"]) / (gained / spent)
        m[3].metric("Elapsed · est. remaining", f"{fmt_duration(elapsed)} · {fmt_duration(remaining)}")
    for note in notes:
        st.warning(note)
    if job and job.error:
        st.error(f"The run stopped with an error: {job.error}")
    elif run.get("error"):
        st.error(run["error"])
    if stats["businesses"] and stats["with_website"] / stats["businesses"] < 0.3:
        st.info(f"Only {pct(stats['with_website'], stats['businesses'])} of these businesses list a "
                "website, so few emails can be found.")

    log_path = cfg.log_dir / f"run_{run_id}.log"
    if log_path.exists():
        with st.expander("Run log"):
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
            st.code("\n".join(lines) or "(empty)", language=None)


# ------------------------------------------------------------------ leads

def page_leads() -> None:
    st.title("Leads")
    runs = store.list_runs(100)
    run_ids = [None] + [r["id"] for r in runs]
    by_id = {r["id"]: r for r in runs}
    default_run = st.session_state.get("leads_run")
    industries = [r[0] for r in store.query(
        "SELECT DISTINCT industry FROM businesses WHERE industry IS NOT NULL ORDER BY 1")]
    cities = [r[0] for r in store.query(
        "SELECT DISTINCT city FROM businesses WHERE city IS NOT NULL AND city != '' ORDER BY 1")]
    rules = load_rules(cfg.industries)

    with st.container(border=True):
        c = st.columns([2, 2, 2, 2])
        run_id = c[0].selectbox("Run", run_ids, index=run_ids.index(default_run) if default_run in run_ids else 0,
                                format_func=lambda i: "All runs" if i is None else run_label(by_id[i]))
        sel_ind = c[1].multiselect("Industry", industries,
                                   format_func=lambda k: industry_label(rules, k))
        sel_city = c[2].multiselect("City", cities)
        statuses = c[3].multiselect("Status", STATUSES, default=["valid"],
                                    format_func=lambda s: STATUS_BADGE[s])
        c = st.columns([2, 2, 1, 1, 1, 1])
        min_score = c[0].slider("Score ≥", 0, 100, int(cfg.get("filters.min_score") or 0))
        types = c[1].multiselect("Email type", EMAIL_TYPES)
        only_new = c[2].toggle("Only new", help="Hide emails that were exported before.")
        per_company = c[3].toggle("One lead per company", value=True,
                                  help="Colleagues at the same company are one lead: the best email, "
                                       "with the others listed alongside. Turn off for one row per email.")
        chains = c[4].toggle("Include chains & large", help="Also show chains, franchises, multinationals "
                             "and well-known companies (normally excluded).")
        activity = st.multiselect(
            "Business activity", ACTIVITY, default=[a for a in (cfg.get("export.activity") or []) if a in ACTIVITY],
            format_func=ACTIVITY_BADGE.get,
            help="Is the business still operating? Active: website online with recent dates, a recently "
                 "updated map listing, or a mailbox confirmed by its mail server. Likely active: website "
                 "online. Unclear: no website and no recent listing. Likely closed: the website's domain "
                 "no longer exists, is parked / for sale, or says the business closed. Empty = all.")
    st.session_state["leads_run"] = run_id

    filters = ExportFilters(statuses=statuses or STATUSES, min_score=min_score, industries=sel_ind,
                            cities=sel_city, email_types=types, run_id=run_id, only_new=only_new,
                            include_chains=chains, mode="company" if per_company else "email",
                            max_contacts=cfg.get("filters.max_contacts_per_company") or None,
                            activity=activity)
    leads = select_leads(store, filters, cfg.email_rules.get("freemail_domains"))
    if not leads:
        st.info("No leads match. Try including risky emails, lowering the minimum score, "
                "or turning off “Only new”.")
        footer()
        return

    df = pd.DataFrame([{
        "Business": l["business_name"], "Email": l["email"],
        "Contact": ("⭐ " if l["decision_maker"] else "") + l["contact_name"],
        "Designation": l["designation"],
        "Owner": ", ".join(x for x in (l["owner_name"], l["owner_title"]) if x),
        "Other contacts": "; ".join(("🟡 " if any(g in c for g in l["guessed"]) else "") + c
                                    for c in l["other_contacts"]),
        "Key people": l["key_people"],
        "Status": STATUS_BADGE.get(l["email_status"]),
        "Type": l["email_type"], "Score": l["score"] or 0, "City": l["city"],
        "Industry": rules[l["industry"]].label if l["industry"] in rules else l["industry"],
        "Activity": ACTIVITY_BADGE.get(l["business_activity"], l["business_activity"]),
        "Last seen": l["email_last_seen"],
        "Phone": l["phone"], "Website": l["website"], "Found in": l["email_found_in"],
    } for l in leads])
    # Green = written on the website / a listing; yellow = guessed and confirmed by the mail server.
    guessed_rows = [l["email"] in l["guessed"] for l in leads]
    styled = df.style.apply(lambda col: [f"background-color: #{EMAIL_YELLOW}; color: #{EMAIL_YELLOW_TEXT}" if g
                                         else f"background-color: #{EMAIL_GREEN}; color: #{EMAIL_GREEN_TEXT}"
                                         for g in guessed_rows], subset=["Email"])
    st.caption("🟩 email written on the business's website or listing · 🟨 / 🟡 guessed from the company's "
               "email format and confirmed by its mail server")
    event = st.dataframe(
        styled, hide_index=True, width="stretch", height=min(560, 38 + 35 * len(df)),
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
            "Website": st.column_config.LinkColumn("Website"),
            "Found in": st.column_config.TextColumn("Found in", help="The website page, or the map "
                                                    "listing with its data provider and date."),
        })

    left, right = st.columns([3, 2])
    st.caption("⭐ = decision maker (owner, founder, CEO, president, managing partner) as named on the "
               "business's own website, or the person the business is named after. Designations of shared "
               "inboxes come from the address (sales@ → Sales). **Owner** is shown even when their own "
               "email isn't published.")
    extra = sum(len(l["other_emails"]) for l in leads)
    left.markdown(f"**{len(leads):,} leads match**" + (
        f" · one per company ({extra:,} colleague emails grouped in)" if per_company and extra else
        "" if per_company else " · one row per email"))
    with right:
        export_controls(filters, leads)

    rows = event.selection.rows if event and event.selection else []
    if rows:
        detail_panel(leads[rows[0]])
    else:
        st.caption("Select a row to see all emails, where they were found, and the score breakdown.")
    footer()


def export_controls(filters: ExportFilters, leads: list[dict]) -> None:
    with st.popover("Export", icon=":material/download:", width="stretch"):
        template = st.selectbox("Column preset", list(TEMPLATES),
                                format_func=lambda t: "LeadScout (all columns)" if t == "default" else t.title())
        columns = None
        if template == "default":
            columns = st.multiselect("Columns", DEFAULT_COLUMNS, default=DEFAULT_COLUMNS)
        fmt = st.radio("Format", ["csv", "xlsx"], horizontal=True,
                       format_func=lambda f: "CSV" if f == "csv" else "Excel")
        name = export_filename(fmt, template)
        data = render_export(leads, fmt=fmt, template=template, columns=columns or None)

        def remember() -> None:
            record_export(store, filters, leads, fmt=fmt, template=template, file_path=f"download:{name}")

        st.download_button(f"Download {len(leads):,} leads", data, file_name=name, type="primary",
                           on_click=remember, width="stretch", icon=":material/download:",
                           mime="text/csv" if fmt == "csv" else
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption("Suppressed emails and domains are always left out. Downloads are remembered "
                   "for “Only new”.")


def detail_panel(lead: dict) -> None:
    b = store.get_business(lead["business_id"])
    with st.container(border=True):
        st.subheader(b["name"])
        addr = ", ".join(p for p in (b["address"], b["city"], b["region"], b["country"]) if p)
        st.caption(addr or "No address on record")
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**Phone** \n{b['phone'] or '–'}")
        site = b["final_url"] or b["website"]
        c2.markdown(f"**Website** \n{f'[{site}]({site})' if site else '–'}")
        c3.markdown(f"**Source** \n{' + '.join(store.business_sources(b['id']))}"
                    + (f" · platform: {b['platform']}" if b["platform"] else ""))
        socials = loads(b["socials"], {}) or {}
        if socials:
            st.markdown(" · ".join(f"[{k.title()}]({v})" for k, v in socials.items()))
        people = loads(b["people"], []) or []
        if people:
            levels = {1: "Owner / CEO", 2: "Executive / Director", 3: "Manager"}
            st.markdown("**People at this company** (from its website, most senior first)")
            st.dataframe(pd.DataFrame([{"Name": x["name"], "Title": x["title"],
                                        "Level": levels.get(x.get("level"), "Staff"),
                                        "Email": x["email"] or "not published"} for x in people]),
                         hide_index=True, width="stretch")
        st.markdown(f"**Activity:** {ACTIVITY_BADGE.get(b['activity'] or 'unclear')}  \n"
                    f":gray[{b['activity_reason'] or 'not checked yet'}]")
        if b["owner_name"]:
            st.markdown(f"**Owner / decision maker:** {b['owner_name']}"
                        + (f", {b['owner_title']}" if b["owner_title"] else "")
                        + f"  \n:gray[Why: {b['owner_basis']}]")
        if b["is_chain"]:
            st.warning(f"Not a small independent business: {b['chain_reason']}")
        if b["error"]:
            st.caption(f"Crawl note: {b['error']}")

        emails = store.emails_for_business(b["id"])
        st.markdown("**All emails found**")
        st.dataframe(pd.DataFrame([{
            "Email": e["email"],
            "Name": ("⭐ " if e["decision_maker"] else "") + (e["contact_name"] or ""),
            "Designation": e["designation"] or "",
            "Status": STATUS_BADGE.get(e["status"], e["status"]),
            "Why": e["status_reason"], "Type": e["email_type"], "Score": e["score"],
            "Found on": e["found_on_url"] or f"({e['source']} listing)",
            "Last seen": e["seen_at"] or "",
        } for e in emails]), hide_index=True, width="stretch")

        detail = loads(next((e["score_detail"] for e in emails if e["email"] == lead["email"]), None), {})
        if detail:
            st.markdown(f"**Score breakdown for {lead['email']}: {sum(detail.values())} / 100**")
            labels = {"email_status": "Email status", "email_type": "Email type",
                      "independent": "Independent (not a chain)", "single_location": "Single location",
                      "has_phone": "Phone present", "website_active": "Website recently updated",
                      "has_social": "Social profile", "small_team": "Small team"}
            weights = cfg.get("scoring")
            maxes = {"email_status": weights["email_valid"], "email_type": weights["email_personal"],
                     "independent": weights["independent"], "single_location": weights["single_location"],
                     "has_phone": weights["has_phone"], "website_active": weights["website_active"],
                     "has_social": weights["has_social"], "small_team": weights["small_team"]}
            st.dataframe(pd.DataFrame([{"Signal": labels.get(k, k), "Points": v, "Max": maxes.get(k, v)}
                                       for k, v in detail.items()]),
                         hide_index=True, width="stretch",
                         column_config={"Points": st.column_config.ProgressColumn(
                             "Points", min_value=0, max_value=25, format="%d")})


# ------------------------------------------------------------------- runs

def page_runs() -> None:
    st.title("Runs")
    runs = store.list_runs(200)
    if not runs:
        st.info("No runs yet. Start one on **New search**.")
        return
    active_ids = {j.run_id for j in registry().active()}
    df = pd.DataFrame([{
        "#": r["id"], "Created": r["created_at"][:16].replace("T", " "), "Kind": r["kind"],
        "Industries": industries_text(r), "Location": r["location_input"],
        "Status": RUN_BADGE["running"] if r["id"] in active_ids else (
            "🟠 Interrupted" if r["status"] == "running" else RUN_BADGE.get(r["status"], r["status"])),
        "Businesses": (r["stats"] or {}).get("businesses"), "Crawled": (r["stats"] or {}).get("crawled"),
        "Emails": (r["stats"] or {}).get("emails"), "Valid": (r["stats"] or {}).get("valid"),
    } for r in runs])
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row")
    rows = event.selection.rows if event and event.selection else []
    if not rows:
        st.caption("Select a run to resume, re-run or view its leads.")
        footer()
        return
    run = runs[rows[0]]
    c = st.columns(7)
    busy = active_job() is not None
    if c[6].button("Check mailboxes", icon=":material/mark_email_read:", width="stretch", disabled=busy,
                   help="Ask the mail servers whether this run's valid-looking company emails really "
                        "exist (no email is sent), then rescore. Needs port 25. Also retries earlier "
                        "'not confirmed' answers."):
        if not port25_status():
            st.error("Port 25 is blocked on this network, so mail servers can't be asked.")
        else:
            start_run(run["id"], "mailboxes")
    if c[5].button("Discard", icon=":material/delete:", width="stretch",
                   disabled=run["id"] in active_ids,
                   help="Pause the run first." if run["id"] in active_ids else
                   "Delete this run and the businesses only it found."):
        discard_dialog(run["id"])
    if c[4].button("Re-check size", icon=":material/domain:", width="stretch", disabled=busy,
                   help="Look for multinationals, well-known companies and non-commercial sites in this "
                        "run (Wikidata + website signals), then rescore. No re-crawling."):
        start_run(run["id"], "recheck")
    if c[0].button("View progress", icon=":material/monitoring:", width="stretch"):
        st.session_state["run_id"] = run["id"]
        st.switch_page(PAGES["progress"])
    if c[1].button("View leads", icon=":material/table:", width="stretch"):
        st.session_state["leads_run"] = run["id"]
        st.switch_page(PAGES["leads"])
    if c[2].button("Resume", icon=":material/play_arrow:", width="stretch",
                   disabled=busy or run["id"] in active_ids or run["status"] not in RESUMABLE):
        start_run(run["id"])
    if c[3].button("Re-run", icon=":material/replay:", width="stretch",
                   disabled=busy or run["kind"] != "search",
                   help="Start a new run with the same search. Known businesses are not duplicated."):
        s = run["settings"]
        f = s.get("filters", {})
        m = manager()
        try:
            new_id = m.create_search_run([r["key"] for r in s.get("industries", [])],
                                         [LocationSpec(**d) for d in s.get("locations", [])],
                                         max_results=f.get("max_results"),
                                         require_website=f.get("require_website"),
                                         exclude_chains=f.get("exclude_chains"))
        finally:
            m.close()
        start_run(new_id)
    footer()


# --------------------------------------------------------------- settings

def page_settings() -> None:
    st.title("Settings")
    tabs = st.tabs(["General", "Crawling", "Scoring", "Suppression list", "Industries", "Chains"])
    with tabs[0]:
        with st.form("general"):
            email = st.text_input("Contact email", cfg.contact_email,
                                  help="Added to the User-Agent so website owners and public services can reach you.")
            ua = st.text_input("User-Agent", cfg.get("user_agent"),
                               help="How LeadScout identifies itself to websites and services.")
            verify_mx = st.checkbox("Check that email domains have a mail server (MX)", cfg.get("email.verify_mx"))
            smtp_check = st.checkbox("Full verification by default (ask mail servers if the mailbox exists)",
                                     cfg.get("email.smtp_check", False),
                                     help="Needs outbound port 25. You can still choose per search.")
            smtp_domain = st.text_input("Your domain for mailbox checks", cfg.get("email.smtp_from_domain") or "",
                                        placeholder="mycompany.com",
                                        help="Mail servers trust checks that introduce themselves with a "
                                             "real domain you own. Empty: the standard empty sender is used.")
            st.checkbox("Google Places (capped)", False, disabled=True, help="Coming in a later phase.")
            if st.form_submit_button("Save", type="primary"):
                save_local_settings(cfg.root, {"contact_email": email.strip(), "user_agent": ua.strip(),
                                           "email": {"verify_mx": verify_mx, "smtp_check": smtp_check,
                                                     "smtp_from_domain": smtp_domain.strip().lstrip("@")}})
                st.success("Saved.")
                st.rerun()
    with tabs[1]:
        c = cfg.get("crawler")
        with st.form("crawl"):
            col = st.columns(2)
            conc = col[0].number_input("Websites in parallel", 1, 64, int(c["global_concurrency"]))
            interval = col[1].number_input("Seconds between requests to the same site", 0.5, 30.0,
                                           float(c["per_domain_min_interval_seconds"]), step=0.5)
            pages = col[0].number_input("Max pages per site", 1, 20, int(c["max_pages_per_site"]))
            timeout = col[1].number_input("Timeout (seconds)", 3, 60, int(c["timeout_seconds"]))
            contacts = st.number_input("Max contacts per company", 1, 20,
                                       int(cfg.get("filters.max_contacts_per_company") or 3),
                                       help="Emails collected and exported per company. The best are kept: "
                                            "named people, then info@-style inboxes, then department inboxes.")
            robots = st.checkbox("Respect robots.txt", bool(c["respect_robots_txt"]),
                                 help="Strongly recommended. Turning this off ignores site owners' wishes.")
            if st.form_submit_button("Save", type="primary"):
                save_local_settings(cfg.root, {"crawler": {
                    "global_concurrency": int(conc), "per_domain_min_interval_seconds": float(interval),
                    "max_pages_per_site": int(pages), "timeout_seconds": int(timeout),
                    "respect_robots_txt": robots},
                    "filters": {"max_contacts_per_company": int(contacts)}})
                st.success("Saved. Applies to the next run.")
                st.rerun()
    with tabs[2]:
        w = cfg.get("scoring")
        labels = {"email_valid": "Email valid", "email_risky": "Email risky",
                  "email_personal": "Personal email", "email_generic": "Generic email (info@)",
                  "email_role": "Role email (sales@)", "independent": "Independent (not a chain)",
                  "single_location": "Single location", "has_phone": "Phone present",
                  "website_active": "Website active", "has_social": "Social profile",
                  "small_team": "Small team"}
        with st.form("scoring"):
            cols = st.columns(3)
            values = {k: cols[i % 3].number_input(label, 0, 100, int(w[k]))
                      for i, (k, label) in enumerate(labels.items())}
            yrs = st.number_input("“Active” means copyright year within N years", 0, 10,
                                  int(w["website_active_years"]))
            st.caption(f"Best possible score with these weights: "
                       f"{values['email_valid'] + values['email_personal'] + sum(values[k] for k in ('independent', 'single_location', 'has_phone', 'website_active', 'has_social', 'small_team'))}")
            if st.form_submit_button("Save", type="primary"):
                save_local_settings(cfg.root, {"scoring": {**values, "website_active_years": int(yrs)}})
                st.success("Saved. Scores update on the next run or resume.")
                st.rerun()
    with tabs[3]:
        suppression_tab()
    with tabs[4]:
        yaml_editor("industries.yaml", "Which Overture Maps categories each industry covers.",
                    validate=lambda text: load_rules(yaml.safe_load(text) or {}))
    with tabs[5]:
        yaml_editor("known_chains.txt", "One chain name or domain per line. Lines starting with # are comments.")
    footer()


def suppression_tab() -> None:
    st.write("Emails and domains on this list never appear in exports.")
    with st.form("suppress", clear_on_submit=True):
        text = st.text_area("Add emails or domains (one per line)")
        reason = st.selectbox("Reason", ["unsubscribed", "bounced", "manual"])
        if st.form_submit_button("Add", type="primary"):
            values = [v.strip() for v in text.replace(",", "\n").splitlines() if v.strip()]
            for v in values:
                store.suppress(v, reason)
            st.success(f"Added {len(values)}.")
    up = st.file_uploader("…or import a CSV/TXT (first column, or an 'email' column)", type=["csv", "txt"])
    if up is not None and st.button("Import file"):
        import csv
        import io
        rows = list(csv.reader(io.StringIO(up.getvalue().decode("utf-8-sig", errors="replace"))))
        header = [h.strip().lower() for h in rows[0]] if rows else []
        col = next((header.index(c) for c in ("email", "domain", "value") if c in header), None)
        body = rows[1:] if col is not None else rows
        n = 0
        for row in body:
            v = row[col or 0].strip() if len(row) > (col or 0) else ""
            if v and ("@" in v or "." in v):
                store.suppress(v, "unsubscribed")
                n += 1
        st.success(f"Imported {n}.")
    rows = store.suppression_list()
    if rows:
        df = pd.DataFrame([{"Value": r["value"], "Kind": r["kind"], "Reason": r["reason"],
                            "Added": r["added_at"][:10]} for r in rows])
        event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                             selection_mode="multi-row")
        picked = event.selection.rows if event and event.selection else []
        if picked and st.button(f"Remove {len(picked)} selected"):
            for i in picked:
                store.unsuppress(rows[i]["value"])
            st.rerun()
    else:
        st.caption("The list is empty.")


def yaml_editor(filename: str, help_text: str, validate=None) -> None:
    path = cfg.root / "config" / filename
    st.write(help_text)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    text = st.text_area(filename, current, height=420, label_visibility="collapsed", key=f"edit-{filename}")
    if st.button("Save", type="primary", key=f"save-{filename}"):
        try:
            if validate:
                validate(text)
        except Exception as exc:  # noqa: BLE001 - show the parse error to the user
            st.error(f"Not saved, the file has an error: {exc}")
            return
        path.write_text(text, encoding="utf-8")
        st.success("Saved.")


# -------------------------------------------------------------- app shell

password_gate()

PAGES = {
    "search": st.Page(page_new_search, title="New search", icon=":material/search:", default=True),
    "progress": st.Page(page_progress, title="Run progress", icon=":material/monitoring:", url_path="progress"),
    "leads": st.Page(page_leads, title="Leads", icon=":material/table:", url_path="leads"),
    "runs": st.Page(page_runs, title="Runs", icon=":material/history:", url_path="runs"),
    "settings": st.Page(page_settings, title="Settings", icon=":material/settings:", url_path="settings"),
}
nav = st.navigation(list(PAGES.values()), position="top")
running = active_job()
if running and nav is not PAGES["progress"]:
    st.info(f"Run #{running.run_id} is running in the background.", icon=":material/sync:")
nav.run()
