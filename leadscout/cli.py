"""LeadScout command-line interface."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from leadscout.config import Config, load_config
from leadscout.db.store import Store
from leadscout.export import TEMPLATES, ExportFilters, select_leads, write_export
from leadscout.industries import (
    NotOnMap,
    UnknownIndustry,
    group_options,
    load_groups,
    load_rules,
    ranked,
    resolve_industry,
)
from leadscout.location import LocationSpec
from leadscout.run_manager import RunManager
from leadscout.sources.csv_import import CSVImportError

app = typer.Typer(help="LeadScout: find small businesses, extract and verify their emails, export CSVs.",
                  no_args_is_help=True, add_completion=False)
suppress_app = typer.Typer(help="Manage the do-not-contact list.", no_args_is_help=True)
app.add_typer(suppress_app, name="suppress")
console = Console()

STAGE_LABELS = {
    "discover": "1. Finding businesses",
    "crawl": "2. Crawled websites ",
    "size": "3. Company size     ",
    "verify": "4. Verified emails  ",
    "mailbox": "4b. Mailbox check   ",
    "guess": "4c. Guessed emails  ",
    "score": "5. Scored           ",
}
STATUS_STYLE = {"valid": "green", "risky": "yellow", "invalid": "red", "unknown": "grey50",
                "done": "green", "running": "cyan", "paused": "yellow", "failed": "red",
                "pending": "grey50"}


class RichProgress:
    def __init__(self, progress: Progress):
        self.progress = progress
        self.tasks: dict[str, int] = {}

    def stage(self, name: str, total: int, done: int = 0) -> None:
        if name in self.tasks:
            self.progress.update(self.tasks[name], total=total, completed=done)
        else:
            self.tasks[name] = self.progress.add_task(STAGE_LABELS.get(name, name),
                                                      total=total, completed=done)

    def advance(self, name: str, n: int = 1) -> None:
        if name in self.tasks:
            self.progress.advance(self.tasks[name], n)

    def note(self, text: str) -> None:
        self.progress.console.print(f"[yellow]![/yellow] {text}")


def _setup() -> tuple[Config, Store]:
    cfg = load_config()
    return cfg, Store(cfg.db_path)


def _warn_contact(cfg: Config) -> None:
    if not cfg.contact_email:
        console.print("[yellow]![/yellow] contact_email is empty in config/settings.yaml. "
                      "Public services (place search, Wikidata) ask for a way to contact you; please set it.")


def _fail(message: str) -> None:
    console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(1)


def _locations(location: list[str], point: Optional[str], radius_km: Optional[float],
               locations_file: Optional[Path]) -> list[LocationSpec]:
    specs: list[LocationSpec] = []
    if point:
        specs.append(LocationSpec("radius", point, radius_km or 10.0))
    names = list(location or [])
    if locations_file:
        if not locations_file.exists():
            _fail(f"Locations file not found: {locations_file}")
        names += [ln.strip() for ln in locations_file.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    for name in names:
        prefix = next((p for p in ("place:", "osm:") if name.lower().startswith(p)), None)
        if prefix:                                    # exact place from 'leadscout places'
            specs.append(LocationSpec("place", name, osm=name[len(prefix):].strip().upper()))
            continue
        specs.append(LocationSpec("radius", name, radius_km) if radius_km else LocationSpec("place", name))
    if not specs:
        _fail("Give a location: --location \"Austin, Texas, USA\", --point LAT,LNG, "
              "or --locations-file cities.txt")
    return specs


def _pct(part: int, whole: int) -> str:
    return f"{(100 * part / whole):.0f}%" if whole else "-"


def _print_summary(run_id: int, stats: dict, status: str, cfg: Config) -> None:
    t = Table(title=f"Run #{run_id} - {status}", show_header=False, box=None, padding=(0, 2))
    if stats.get("found"):
        t.add_row("Businesses found on the map", f"{stats['found']:,}")
    t.add_row("Businesses kept for this run", f"{stats['businesses']:,}")
    t.add_row("  with a website", f"{stats['with_website']:,} ({_pct(stats['with_website'], stats['businesses'])})")
    t.add_row("  social-media page only", f"{stats['social_only']:,}")
    t.add_row("  chains & large companies", f"{stats['chains']:,}")
    t.add_row("Websites crawled", f"{stats['crawled']:,} of {stats['with_website']:,}")
    t.add_row("Businesses with an email", f"{stats['with_email']:,} ({_pct(stats['with_email'], stats['businesses'])})")
    t.add_row("Emails found", f"{stats['emails']:,}")
    t.add_row("  [green]valid[/green] / [yellow]risky[/yellow] / [red]invalid[/red] / unknown",
              f"{stats['valid']:,} / {stats['risky']:,} / {stats['invalid']:,} / {stats['unknown']:,}")
    if stats.get("mailbox_checked"):
        t.add_row("  mailbox confirmed by mail server",
                  f"{stats['mailbox_confirmed']:,} of {stats['mailbox_checked']:,} asked")
    t.add_row("Errors (see log)", f"{stats['errors']:,}  {cfg.log_dir / f'run_{run_id}.log'}")
    console.print(t)
    if stats["businesses"] and stats["with_website"] / stats["businesses"] < 0.3:
        console.print(f"[yellow]![/yellow] Only {_pct(stats['with_website'], stats['businesses'])} of these "
                      "businesses list a website, so few emails can be found.")


def _execute(manager: RunManager, run_id: int, cfg: Config, store: Store) -> None:
    progress = Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                        TimeElapsedColumn(), console=console)
    manager.progress = RichProgress(progress)
    try:
        with progress:
            stats = manager.execute(run_id)
    except KeyboardInterrupt:
        stats = manager.stats(run_id)
    except Exception as exc:  # noqa: BLE001
        _fail(f"Run #{run_id} failed: {exc}. Details in {cfg.log_dir / f'run_{run_id}.log'}")
        return
    finally:
        manager.close()
    status = store.get_run(run_id)["status"]
    _print_summary(run_id, stats, status, cfg)
    if status == "paused":
        console.print(f"Paused. Continue with: [bold]leadscout resume {run_id}[/bold]")
    elif status == "done":
        console.print(f"Next: [bold]leadscout export --run {run_id}[/bold]  "
                      "(add --status risky to include risky emails)")


# ---------------------------------------------------------------- commands

@app.command()
def industries(
    group: Optional[str] = typer.Option(None, "--group", "-g", help="Only this section, e.g. healthcare."),
) -> None:
    """List the 50 industries, ranked (construction first)."""
    cfg = load_config()
    rules = load_rules(cfg.industries)
    groups = load_groups(cfg.industries)
    t = Table("Rank", "Section", "Industry (use with -i)", "Label")
    for rule in ranked(rules):
        if group and rule.group != group.strip().lower():
            continue
        t.add_row(str(rule.rank or "-"), groups.get(rule.group or "", rule.group or "-"), rule.key, rule.label)
    console.print(t)
    console.print("Sections: " + "; ".join(f"[bold]group:{g}[/bold] = {d}" for g, d in group_options(rules, groups))
                  + "; [bold]topN[/bold] = ranks 1 to N (e.g. top10).")
    console.print("Example: leadscout run -i construction -l \"Austin, Texas, USA\" --max 300")


IndustryOpt = typer.Option(..., "--industry", "-i", help="Industry, e.g. construction or dental. Repeatable.")
LocationOpt = typer.Option(None, "--location", "-l", help="Place name, e.g. 'Austin, Texas, USA'. Repeatable.")
VerifyOpt = typer.Option(None, "--verify", help="basic (syntax + mail domain) or full (also ask the mail "
                                                "server if the mailbox exists; needs port 25). "
                                                "Default: email.smtp_check in settings.")
PointOpt = typer.Option(None, "--point", help="Centre point 'lat,lng' for a radius search.")
RadiusOpt = typer.Option(None, "--radius-km", help="Search radius in km (with --point or --location).")
FileOpt = typer.Option(None, "--locations-file", help="Text file with one place per line (batch).")


@app.command()
def places(query: str = typer.Argument(..., help="Place to look up, e.g. 'Springfield'.")) -> None:
    """Find the exact place to search (e.g. which Springfield), then use it with -l place:ID."""
    from leadscout.location import LocationError, LocationResolver

    cfg, store = _setup()
    resolver = LocationResolver(cfg, store)
    try:
        found = resolver.suggest(query)
    except LocationError as exc:
        _fail(str(exc))
        return
    finally:
        resolver.close()
    if not found:
        _fail(f"No place called '{query}' found. Check the spelling or add the country.")
    t = Table("Use with -l", "Type", "Place")
    for f in found:
        t.add_row(f"place:{f['osm']}", f["kind"], f["label"])
    console.print(t)
    console.print(f"Example: leadscout run -i construction -l place:{found[0]['osm']}")


@app.command()
def preview(
    industry: list[str] = IndustryOpt,
    location: Optional[list[str]] = LocationOpt,
    point: Optional[str] = PointOpt,
    radius_km: Optional[float] = RadiusOpt,
    locations_file: Optional[Path] = FileOpt,
) -> None:
    """Count the businesses in a place (no website visits) before starting a run."""
    cfg, store = _setup()
    _warn_contact(cfg)
    specs = _locations(location or [], point, radius_km, locations_file)
    manager = RunManager(cfg, store)
    try:
        with console.status("Downloading businesses from Overture Maps..."):
            rows = manager.preview(industry, specs)
    except (UnknownIndustry, NotOnMap) as exc:
        _fail(str(exc))
    finally:
        manager.close()
    t = Table("Location", "Rank", "Industry", "Businesses", "With website", "Email listed",
              "Brands / chains")
    total = with_site = empty = 0
    many = len(rows) > 5
    for r in rows:
        if "error" in r:
            t.add_row(r["location"], "", r.get("industry", "-"), f"[red]{r['error']}[/red]", "", "", "")
            continue
        total += r["businesses"]
        with_site += r["with_website"]
        if many and not r["businesses"]:
            empty += 1
            continue
        t.add_row(r["location"], str(r.get("rank") or ""), r["industry"], f"{r['businesses']:,}",
                  f"{r['with_website']:,} ({_pct(r['with_website'], r['businesses'])})",
                  f"{r['with_email']:,}", f"{r['branded']:,}")
    console.print(t)
    console.print(f"[bold]Total: {total:,} businesses, {with_site:,} with a website[/bold]"
                  + (f"  [dim]({empty} industries with no results hidden)[/dim]" if empty else ""))
    for note in dict.fromkeys(r["note"] for r in rows if r.get("note")):
        console.print(f"[yellow]![/yellow] {note}")
    for loc, resolved in {r["location"]: r["resolved"] for r in rows if r.get("resolved")}.items():
        console.print(f"[dim]{loc} -> {resolved}[/dim]")
    if rows and total == 0:
        console.print("No businesses found. Try a nearby larger city, a radius search, "
                      "or another industry word.")


@app.command()
def run(
    industry: list[str] = IndustryOpt,
    location: Optional[list[str]] = LocationOpt,
    point: Optional[str] = PointOpt,
    radius_km: Optional[float] = RadiusOpt,
    locations_file: Optional[Path] = FileOpt,
    max_results: Optional[int] = typer.Option(None, "--max", help="Max businesses in this run."),
    include_chains: bool = typer.Option(False, "--include-chains", help="Also crawl chains/franchises."),
    allow_no_website: bool = typer.Option(False, "--allow-no-website",
                                          help="Keep businesses without a website in the run."),
    verify: Optional[str] = VerifyOpt,
) -> None:
    """Search, crawl, verify and score. Ctrl+C pauses; 'resume' continues."""
    cfg, store = _setup()
    _warn_contact(cfg)
    specs = _locations(location or [], point, radius_km, locations_file)
    manager = RunManager(cfg, store)
    try:
        run_id = manager.create_search_run(
            industry, specs, max_results=max_results,
            require_website=False if allow_no_website else None,
            exclude_chains=False if include_chains else None, verification=verify)
    except (UnknownIndustry, ValueError) as exc:
        manager.close()
        _fail(str(exc))
        return
    console.print(f"Run [bold]#{run_id}[/bold]: {', '.join(industry)} in "
                  f"{'; '.join(s.label() for s in specs)}  [dim](Ctrl+C to pause)[/dim]")
    _execute(manager, run_id, cfg, store)


@app.command()
def enrich(
    file: Path = typer.Argument(..., help="CSV with a website column (optional: name, phone, city, country)."),
    industry: Optional[str] = typer.Option(None, "--industry", "-i", help="Industry label for these rows."),
    verify: Optional[str] = VerifyOpt,
) -> None:
    """Crawl and verify an existing list of websites."""
    cfg, store = _setup()
    manager = RunManager(cfg, store)
    try:
        run_id = manager.create_enrich_run(file, industry, verification=verify)
    except (CSVImportError, UnknownIndustry, ValueError) as exc:
        manager.close()
        _fail(str(exc))
        return
    console.print(f"Run [bold]#{run_id}[/bold]: enriching {store.run_business_count(run_id):,} "
                  f"websites from {file.name}")
    _execute(manager, run_id, cfg, store)


@app.command()
def resume(run_id: int = typer.Argument(..., help="Run number from 'leadscout runs'.")) -> None:
    """Continue a paused or failed run where it stopped."""
    cfg, store = _setup()
    run_row = store.get_run(run_id)
    if not run_row:
        _fail(f"Run #{run_id} does not exist. See 'leadscout runs'.")
    console.print(f"Resuming run [bold]#{run_id}[/bold] ({run_row['status']}, "
                  f"stopped at stage: {run_row['stage'] or 'start'})")
    _execute(RunManager(cfg, store), run_id, cfg, store)


@app.command()
def recheck(run_id: int = typer.Argument(..., help="Run number from 'leadscout runs'.")) -> None:
    """Re-check a finished run for chains, multinationals and non-commercial sites, then rescore."""
    cfg, store = _setup()
    if not store.get_run(run_id):
        _fail(f"Run #{run_id} does not exist. See 'leadscout runs'.")
    _warn_contact(cfg)
    manager = RunManager(cfg, store)
    progress = Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                        TimeElapsedColumn(), console=console)
    manager.progress = RichProgress(progress)
    before = {r["id"] for r in store.run_businesses(run_id, "b.is_chain = 1")}
    try:
        with progress:
            manager.recheck(run_id)
    finally:
        manager.close()
    flagged = store.run_businesses(run_id, "b.is_chain = 1")
    new = [r for r in flagged if r["id"] not in before]
    console.print(f"Run #{run_id}: {len(flagged):,} chains / large companies flagged ({len(new):,} new).")
    for r in new[:40]:
        console.print(f"  - {r['name']} ({r['domain'] or 'no website'}): {r['chain_reason']}")
    if len(new) > 40:
        console.print(f"  ... and {len(new) - 40} more")
    console.print("They are left out of exports unless you add --include-chains.")


@app.command()
def mailboxes(run_id: int = typer.Argument(..., help="Run number from 'leadscout runs'.")) -> None:
    """Ask the mail servers whether a run's valid-looking emails really exist (no email is sent)."""
    from leadscout.verify.smtp_check import port25_open

    cfg, store = _setup()
    if not store.get_run(run_id):
        _fail(f"Run #{run_id} does not exist. See 'leadscout runs'.")
    if not port25_open():
        _fail("Outbound port 25 is blocked on this network, so mail servers can't be asked. "
              "Most home internet providers and cloud hosts block it; a VPS that allows port 25 works.")
    manager = RunManager(cfg, store)
    progress = Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                        TimeElapsedColumn(), console=console)
    manager.progress = RichProgress(progress)
    before = store.run_stats(run_id)
    try:
        with progress:
            stats = manager.check_mailboxes(run_id)
    finally:
        manager.close()
    asked = stats["mailbox_checked"] - before["mailbox_checked"]
    console.print(f"Run #{run_id}: {stats['mailbox_checked']:,} addresses asked ({asked:,} now). "
                  f"Confirmed: {stats['mailbox_confirmed']:,}. Now valid / risky / invalid / unknown: "
                  f"{stats['valid']:,} / {stats['risky']:,} / {stats['invalid']:,} / {stats['unknown']:,}")
    if stats["mailbox_todo"]:
        console.print(f"[yellow]![/yellow] {stats['mailbox_todo']:,} not answered yet; run it again later.")


@app.command()
def dedupe(backup: bool = typer.Option(True, help="Copy the database to data/ before changing it.")) -> None:
    """Merge duplicate companies already in the database (runs do this automatically)."""
    cfg, store = _setup()
    if backup:
        from datetime import datetime
        dest = cfg.db_path.with_name(f"{cfg.db_path.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}.db")
        target = __import__("sqlite3").connect(dest)
        store.conn.backup(target)
        target.close()
        console.print(f"Backup saved: {dest}")
    before = store.scalar("SELECT COUNT(*) FROM businesses")
    manager = RunManager(cfg, store)
    try:
        with console.status("Looking for duplicate companies..."):
            merges = manager.dedupe_database()
    finally:
        manager.close()
    after = store.scalar("SELECT COUNT(*) FROM businesses")
    for m in merges[:60]:
        console.print(f"  - '{m.dropped}' -> '{m.kept}': {m.reason}")
    if len(merges) > 60:
        console.print(f"  ... and {len(merges) - 60} more (see the logs)")
    console.print(f"Companies: {before:,} -> {after:,} ({len(merges):,} duplicates merged). "
                  "Emails, sources and run links were kept.")


@app.command()
def discard(
    run_id: int = typer.Argument(..., help="Run number from 'leadscout runs'."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
) -> None:
    """Delete a run and the businesses/emails that only it found."""
    _, store = _setup()
    run_row = store.get_run(run_id)
    if not run_row:
        _fail(f"Run #{run_id} does not exist. See 'leadscout runs'.")
    counts = store.discard_preview(run_id)
    console.print(f"Run #{run_id}: {', '.join(run_row['industries'][:3])}"
                  f"{'…' if len(run_row['industries']) > 3 else ''} · {run_row['location_input']}")
    console.print(f"Removes the run, {counts['businesses']:,} businesses and {counts['emails']:,} emails "
                  f"found only by it ({counts['shared']:,} shared businesses are kept).")
    if not yes and not typer.confirm("Discard it?"):
        raise typer.Exit()
    store.discard_run(run_id)
    console.print(f"Run #{run_id} discarded.")


@app.command()
def runs(limit: int = typer.Option(20, help="How many recent runs to show.")) -> None:
    """List past runs."""
    _, store = _setup()
    rows = store.list_runs(limit)
    if not rows:
        console.print("No runs yet. Start one with: leadscout run -i dentist -l \"Austin, Texas, USA\"")
        return
    t = Table("#", "Created", "Kind", "Industries", "Location", "Status", "Businesses",
              "Crawled", "Emails", "Valid")
    for r in rows:
        s = r["stats"] or {}
        style = STATUS_STYLE.get(r["status"], "")
        t.add_row(str(r["id"]), r["created_at"][:16].replace("T", " "), r["kind"],
                  ", ".join(r["industries"]) or "-", r["location_input"][:40],
                  f"[{style}]{r['status']}[/{style}]" if style else r["status"],
                  str(s.get("businesses", "")), str(s.get("crawled", "")),
                  str(s.get("emails", "")), str(s.get("valid", "")))
    console.print(t)


def _export_filters(cfg: Config, status, min_score, industry, city, email_type, run_id,
                    only_new, include_chains, per_email, activity=None) -> ExportFilters:
    statuses = [s.lower() for s in (status or cfg.get("export.default_statuses") or ["valid"])]
    bad = [s for s in statuses if s not in ("valid", "risky", "invalid", "unknown")]
    if bad:
        _fail(f"Unknown status {bad[0]!r}. Use valid, risky, invalid or unknown.")
    rules = load_rules(cfg.industries)
    industries = []
    for word in industry or []:
        try:
            industries.append(resolve_industry(word, rules).key)
        except UnknownIndustry:
            industries.append(word)
    return ExportFilters(
        statuses=statuses,
        min_score=min_score if min_score is not None else int(cfg.get("filters.min_score") or 0),
        industries=industries, cities=list(city or []), email_types=list(email_type or []),
        run_id=run_id, only_new=only_new, include_chains=include_chains,
        mode="email" if per_email else cfg.get("export.mode", "company"),
        max_contacts=cfg.get("filters.max_contacts_per_company") or None,
        activity=_activity(cfg, activity),
    )


ACTIVITY_WORDS = {"active": "active", "likely": "likely active", "likely-active": "likely active",
                  "likely active": "likely active", "unclear": "unclear", "closed": "likely closed",
                  "likely-closed": "likely closed", "likely closed": "likely closed"}


def _activity(cfg: Config, words) -> list[str]:
    if words and any(w.lower() == "all" for w in words):
        return []
    if not words:
        return list(cfg.get("export.activity") or [])
    out = []
    for w in words:
        if w.lower() not in ACTIVITY_WORDS:
            _fail(f"Unknown activity {w!r}. Use active, likely-active, unclear, likely-closed or all.")
        out.append(ACTIVITY_WORDS[w.lower()])
    return out


StatusOpt = typer.Option(None, "--status", "-s", help="valid / risky / invalid / unknown. Repeatable. Default: valid.")
ScoreOpt = typer.Option(None, "--min-score", help="Only leads scoring at least this.")
RunOpt = typer.Option(None, "--run", help="Only leads from this run.")
ActivityOpt = typer.Option(None, "--activity", help="Business activity: active, likely-active, unclear, "
                           "likely-closed, or all. Repeatable. Default: export.activity in settings "
                           "(active + likely active).")
TypeOpt = typer.Option(None, "--type", help="Email type: personal / generic / role. Repeatable.")


@app.command()
def export(
    status: Optional[list[str]] = StatusOpt,
    min_score: Optional[int] = ScoreOpt,
    industry: Optional[list[str]] = typer.Option(None, "--industry", "-i"),
    city: Optional[list[str]] = typer.Option(None, "--city"),
    email_type: Optional[list[str]] = TypeOpt,
    run_id: Optional[int] = RunOpt,
    only_new: bool = typer.Option(False, "--only-new", help="Skip companies (or, with --per-email, emails) exported before."),
    per_email: bool = typer.Option(False, "--per-email",
                                   help="One row per email address instead of one lead per company."),
    best: bool = typer.Option(False, "--best", hidden=True, help="Old flag; one lead per company is now the default."),
    include_chains: bool = typer.Option(False, "--include-chains"),
    activity: Optional[list[str]] = ActivityOpt,
    fmt: Optional[str] = typer.Option(None, "--format", "-f", help="csv or xlsx."),
    template: str = typer.Option("default", "--template", "-t",
                                 help=f"Column preset: {', '.join(TEMPLATES)}."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path."),
) -> None:
    """Export leads to CSV or Excel. Suppressed emails are always excluded."""
    cfg, store = _setup()
    filters = _export_filters(cfg, status, min_score, industry, city, email_type, run_id,
                              only_new, include_chains, per_email, activity)
    try:
        path, count = write_export(store, filters, export_dir=cfg.export_dir,
                                   fmt=(fmt or cfg.get("export.format", "csv")).lower(),
                                   template=template, output=output,
                                   freemail_domains=cfg.email_rules.get("freemail_domains"))
    except ValueError as exc:
        _fail(str(exc))
        return
    unit = "email rows" if filters.mode == "email" else "leads (one per company)"
    console.print(f"Exported [bold]{count:,}[/bold] {unit} to {path}")
    if count == 0:
        console.print("[dim]Nothing matched. Try --status risky, a lower --min-score, "
                      "or drop --only-new.[/dim]")


@app.command()
def leads(
    status: Optional[list[str]] = StatusOpt,
    min_score: Optional[int] = ScoreOpt,
    industry: Optional[list[str]] = typer.Option(None, "--industry", "-i"),
    city: Optional[list[str]] = typer.Option(None, "--city"),
    email_type: Optional[list[str]] = TypeOpt,
    run_id: Optional[int] = RunOpt,
    include_chains: bool = typer.Option(False, "--include-chains"),
    activity: Optional[list[str]] = ActivityOpt,
    per_email: bool = typer.Option(False, "--per-email", help="One row per email address."),
    limit: int = typer.Option(25, help="Rows to show."),
) -> None:
    """Show leads in the terminal (same filters as export)."""
    cfg, store = _setup()
    filters = _export_filters(cfg, status, min_score, industry, city, email_type, run_id,
                              False, include_chains, per_email, activity)
    rows = select_leads(store, filters, cfg.email_rules.get("freemail_domains"))
    t = Table("Business", "Email", "Contact / designation", "+ colleagues", "Status", "Activity", "Score", "City",
              "Found in / last seen")
    for r in rows[:limit]:
        style = STATUS_STYLE.get(r["email_status"], "")
        who = ", ".join(x for x in (("* " if r["decision_maker"] else "") + r["contact_name"], r["designation"]) if x.strip())
        t.add_row(r["business_name"][:30], r["email"], who[:40], str(len(r["other_emails"]) or ""),
                  f"[{style}]{r['email_status']}[/{style}]",
                  r["business_activity"], str(r["score"]), r["city"][:18],
                  f"{r['email_found_in'][:40]} {r['email_last_seen']}")
    console.print(t)
    unit = "email rows" if filters.mode == "email" else "leads (one per company)"
    console.print(f"{len(rows):,} {unit} match" + (f" (showing {limit})" if len(rows) > limit else ""))


# ------------------------------------------------------------- suppression

@suppress_app.command("add")
def suppress_add(
    values: list[str] = typer.Argument(..., help="Emails or domains."),
    reason: str = typer.Option("manual", help="unsubscribed / bounced / manual"),
) -> None:
    """Never export these emails or domains again."""
    _, store = _setup()
    for value in values:
        kind = store.suppress(value, reason)
        console.print(f"Suppressed {kind}: {value.strip().lower()}")


@suppress_app.command("remove")
def suppress_remove(values: list[str] = typer.Argument(...)) -> None:
    """Remove entries from the do-not-contact list."""
    _, store = _setup()
    for value in values:
        ok = store.unsuppress(value)
        console.print(("Removed " if ok else "Not on the list: ") + value)


@suppress_app.command("list")
def suppress_list() -> None:
    """Show the do-not-contact list."""
    _, store = _setup()
    rows = store.suppression_list()
    t = Table("Value", "Kind", "Reason", "Added")
    for r in rows:
        t.add_row(r["value"], r["kind"], r["reason"] or "", r["added_at"][:10])
    console.print(t)
    console.print(f"{len(rows):,} entries")


@suppress_app.command("import")
def suppress_import(
    file: Path = typer.Argument(..., help="CSV/TXT with emails or domains (first column, or an 'email' column)."),
    reason: str = typer.Option("unsubscribed"),
) -> None:
    """Import unsubscribes / bounces from a file."""
    _, store = _setup()
    if not file.exists():
        _fail(f"File not found: {file}")
    count = 0
    with file.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        _fail("The file is empty.")
    header = [h.strip().lower() for h in rows[0]]
    col = next((header.index(c) for c in ("email", "domain", "value") if c in header), None)
    body = rows[1:] if col is not None else rows
    col = col or 0
    for row in body:
        if len(row) > col and row[col].strip() and ("@" in row[col] or "." in row[col]):
            store.suppress(row[col], reason)
            count += 1
    console.print(f"Imported {count:,} entries into the suppression list.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
