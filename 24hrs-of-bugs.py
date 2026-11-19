#!/usr/bin/env python3
#
# Scan Launchpad for newly opened bugs filed against Ubuntu kernel targets,
# maintain a persistent per-bug JSON store, and render static HTML reports
# for the 24 hour, 7 day, 30 day and 90 day windows.
#
# This is a rewrite of the original stable/24hrs-of-bugs email reporter. It is
# designed to run as a time-triggered Forgejo action: the JSON store and the
# rendered pages live in git and are regenerated incrementally on every run.

import json
import logging
import os
import random
import socket
import time
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.error import HTTPError as UrllibHTTPError
from urllib.error import URLError

from launchpadlib.launchpad import Launchpad

try:
    from lazr.restfulclient.errors import HTTPError as LazrHTTPError
    from lazr.restfulclient.errors import ServerError as LazrServerError
except ImportError:  # pragma: no cover - lazr always ships with launchpadlib
    LazrHTTPError = ()
    LazrServerError = ()

from cktools_data import KernelSeries, KernelTargets

log = logging.getLogger("24hrs-of-bugs")

# Bug task statuses we care about (mirrors the original reporter).
SEARCH_STATUS = [
    "New",
    "Incomplete (with response)",
    "Incomplete (without response)",
    "Confirmed",
    "Triaged",
    "In Progress",
    "Fix Committed",
    "Invalid",
    "Fix Released",
]

# Reporting windows (label -> number of days).
WINDOWS = [
    ("24-hours", "last 24 hours", 1),
    ("7-days", "last 7 days", 7),
    ("30-days", "last 30 days", 30),
    ("90-days", "last 90 days", 90),
]

# How long bugs are retained in the store (matches the widest window).
RETENTION_DAYS = 90

# Overlap subtracted from the last-run timestamp to absorb clock skew and
# Launchpad's eventual consistency.
FUDGE_MINUTES = 15

# Retry policy for flaky Launchpad calls.
RETRY_ATTEMPTS = 5
RETRY_BASE_SECONDS = 2
RETRY_CAP_SECONDS = 60

# Errors that are worth retrying.
RETRYABLE_EXCEPTIONS = (URLError, socket.timeout, socket.error, ConnectionError)
if LazrServerError:
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (LazrServerError,)


class RetryExhausted(Exception):
    """Raised when a Launchpad operation keeps failing after all retries."""


def _is_retryable(exc):
    """Return True if the exception should be retried."""
    if isinstance(exc, RETRYABLE_EXCEPTIONS):
        return True
    # lazr and urllib HTTP errors: retry on 429 and 5xx, give up on other 4xx.
    code = getattr(exc, "response", None)
    code = getattr(code, "status", None)
    if code is None:
        code = getattr(exc, "code", None)
    if code is not None:
        code = int(code)
        return code == 429 or 500 <= code <= 599
    return False


def _retry_after(exc):
    """Return a server-requested delay in seconds, if any."""
    headers = getattr(exc, "response", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def retry(description, func):
    """Call func() with exponential backoff + jitter, honouring Retry-After.

    Raises RetryExhausted when all attempts fail with a retryable error, and
    re-raises immediately for non-retryable errors.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - we classify below
            if isinstance(exc, (LazrHTTPError, UrllibHTTPError)) and not _is_retryable(exc):
                raise
            if not _is_retryable(exc) and not isinstance(exc, (LazrHTTPError, UrllibHTTPError)):
                raise
            if attempt == RETRY_ATTEMPTS:
                raise RetryExhausted(f"{description}: giving up after {attempt} attempts: {exc}") from exc

            delay = _retry_after(exc)
            if delay is None:
                delay = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
                delay *= 1 + random.uniform(-0.2, 0.2)
            log.warning("WW: %s failed (attempt %d/%d): %s; retrying in %.1fs",
                        description, attempt, RETRY_ATTEMPTS, exc, delay)
            time.sleep(delay)


def iso(dt):
    """Serialise a datetime as a UTC ISO-8601 string."""
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    """Parse an ISO-8601 string into an aware UTC datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Store:
    """Filesystem-backed store of per-bug JSON files plus a last-run stamp."""

    def __init__(self, root):
        self.root = root
        self.bugs_dir = os.path.join(root, "bugs")
        self.last_run_path = os.path.join(root, "last-run.json")
        os.makedirs(self.bugs_dir, exist_ok=True)

    def read_last_run(self):
        try:
            with open(self.last_run_path) as fh:
                return parse_iso(json.load(fh)["last_run"])
        except (FileNotFoundError, KeyError, ValueError):
            return None

    def write_last_run(self, when):
        tmp = self.last_run_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"last_run": iso(when)}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.last_run_path)

    def _bug_path(self, bug_id):
        return os.path.join(self.bugs_dir, f"{bug_id}.json")

    def load_bug(self, bug_id):
        try:
            with open(self._bug_path(bug_id)) as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError):
            return None

    def save_bug(self, record):
        path = self._bug_path(record["id"])
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)

    def all_bugs(self):
        for name in sorted(os.listdir(self.bugs_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.bugs_dir, name)) as fh:
                    yield json.load(fh)
            except ValueError:
                log.warning("WW: Skipping unreadable bug file: %s", name)

    def remove_bug(self, bug_id):
        try:
            os.remove(self._bug_path(bug_id))
        except FileNotFoundError:
            pass


class Scanner:
    """Drives the Launchpad scan and store upsert."""

    def __init__(self, args, store):
        self.args = args
        self.store = store
        self.now = args.now
        self.kernel_series = KernelSeries()
        # Codenames, newest series first, so the most recent matching tag wins.
        self._series_codenames = [
            s.codename
            for s in sorted(self.kernel_series.series, key=KernelSeries.key_series_name, reverse=True)
            if s.codename
        ]

        self.can_tag = False
        if args.credentials_file and os.path.exists(args.credentials_file):
            log.info("II: Launchpad login with credentials")
            self.lp = Launchpad.login_with(
                "24hrs-of-bugs", "production", version="devel",
                credentials_file=args.credentials_file,
            )
            self.can_tag = not args.no_tag
        else:
            log.info("II: Launchpad login anonymously")
            self.lp = Launchpad.login_anonymously(
                "24hrs-of-bugs", "production", ".shadow-cache", version="devel"
            )
        self.distro = self.lp.distributions["ubuntu"]

        # Per-run cache of bug.tags keyed by bug id, so a bug hit via several
        # targets is only dereferenced once.
        self._tags_cache = {}
        self.failures = 0

    def series_from_tags(self, tags):
        for codename in self._series_codenames:
            if codename in tags:
                return codename
        return ""

    def _entry_for(self, name, type_):
        if type_ == "project":
            try:
                return self.lp.projects[name]
            except KeyError:
                log.warning("WW: Project not found: %s", name)
                return None
        entry = self.distro.getSourcePackage(name=name)
        if entry is None:
            log.warning("WW: Source package not found: %s", name)
        return entry

    def _bug_tags(self, bug):
        bug_id = bug.id
        if bug_id not in self._tags_cache:
            self._tags_cache[bug_id] = retry(f"bug {bug_id} tags", lambda: list(bug.tags))
        return self._tags_cache[bug_id]

    def scan_target(self, name, type_, since):
        """Query one target and upsert every in-window task into the store."""
        entry = retry(f"lookup {type_} {name}", lambda: self._entry_for(name, type_))
        if entry is None:
            return

        tasks = retry(
            f"searchTasks {type_} {name}",
            lambda: entry.searchTasks(status=SEARCH_STATUS, modified_since=since),
        )

        # Iterate defensively: each page is a separate HTTP fetch.
        iterator = iter(tasks)
        while True:
            try:
                task = retry(f"page of {type_} {name}", lambda: next(iterator, None))
            except StopIteration:
                break
            if task is None:
                break

            bug = task.bug
            tags = self._bug_tags(bug)
            if "kernel-release-tracking-bug" in tags:
                continue

            date_created = task.date_created.astimezone(timezone.utc)
            # Ignore tasks whose creation predates the retention window; the
            # pruner would drop them again immediately.
            if date_created < self.now - timedelta(days=RETENTION_DAYS):
                continue

            self._upsert(bug, task, tags, name, type_, date_created)

    def _upsert(self, bug, task, tags, name, type_, date_created):
        bug_id = bug.id
        record = self.store.load_bug(bug_id) or {
            "id": bug_id,
            "targets": [],
        }

        try:
            assignee = task.assignee.display_name
        except AttributeError:
            assignee = "unassigned"
        except ValueError:
            assignee = "unknown"

        record["title"] = bug.title
        record["series"] = self.series_from_tags(tags)
        record["importance"] = task.importance
        record["status"] = task.status
        record["assignee"] = assignee
        record["last_seen"] = iso(self.now)

        target = {"name": name, "type": type_, "date_created": iso(date_created)}
        existing = next((t for t in record["targets"] if t["name"] == name and t["type"] == type_), None)
        if existing:
            existing["date_created"] = target["date_created"]
        else:
            record["targets"].append(target)
        record["targets"].sort(key=lambda t: (t["name"], t["type"]))

        self.store.save_bug(record)
        log.info("II: Upserted LP: #%s (%s) -- %s -- %s", bug_id, name, task.status, bug.title)

        if self._should_tag(date_created):
            self._tag(bug, tags, name)

    def _should_tag(self, date_created):
        return self.can_tag and date_created >= self.now - timedelta(days=1)

    def _tag(self, bug, tags, name):
        new_tags = []
        if "kernel-daily-bug" not in tags:
            new_tags.append("kernel-daily-bug")
        if name.startswith("linux-firmware") and "linux-firmware-daily-bug" not in tags:
            new_tags.append("linux-firmware-daily-bug")
        if not new_tags:
            return
        log.info("II: Tagging LP: #%s with %s", bug.id, new_tags)

        def _save():
            bug.tags = tags + new_tags
            bug.lp_save()

        try:
            retry(f"tag bug {bug.id}", _save)
            self._tags_cache[bug.id] = tags + new_tags
        except Exception as exc:  # noqa: BLE001 - tagging is best-effort
            log.warning("WW: Failed to tag LP: #%s: %s", bug.id, exc)

    def run(self):
        last_run = self.store.read_last_run()
        if last_run is None:
            since = self.now - timedelta(days=self.args.seed_days)
            log.info("II: No last-run stamp; seeding from %s", iso(since))
        else:
            since = last_run - timedelta(minutes=self.args.fudge_minutes)
            log.info("II: Incremental scan since %s (last run %s)", iso(since), iso(last_run))

        targets = sorted({(t.name, t.type) for t in KernelTargets().all})
        log.info("II: Scanning %d targets", len(targets))
        for name, type_ in targets:
            try:
                self.scan_target(name, type_, since)
            except RetryExhausted as exc:
                self.failures += 1
                log.warning("WW: Target %s (%s) failed: %s", name, type_, exc)
            except (LazrHTTPError, UrllibHTTPError) as exc:
                self.failures += 1
                log.warning("WW: Target %s (%s) failed: %s", name, type_, exc)

        self.prune()

        if self.failures:
            log.warning("WW: %d target(s) failed; NOT advancing last-run stamp", self.failures)
        else:
            self.store.write_last_run(self.now)
            log.info("II: Advanced last-run stamp to %s", iso(self.now))

    def prune(self):
        cutoff = self.now - timedelta(days=RETENTION_DAYS)
        removed = 0
        for record in self.store.all_bugs():
            newest = max(parse_iso(t["date_created"]) for t in record["targets"])
            if newest < cutoff:
                self.store.remove_bug(record["id"])
                removed += 1
        if removed:
            log.info("II: Pruned %d bug(s) older than %d days", removed, RETENTION_DAYS)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

PAGE_CSS = """
:root { --orange: #E95420; --aubergine: #772953; --ink: #111; --muted: #666; }
* { box-sizing: border-box; }
body {
  margin: 0; color: var(--ink);
  font-family: Ubuntu, "Ubuntu", -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  background: #fff; line-height: 1.5;
}
header.k-head {
  background: var(--aubergine); color: #fff; padding: 1rem 2rem;
  display: flex; align-items: center; gap: 0.75rem;
}
header.k-head .k-logo {
  width: 1.75rem; height: 1.75rem; background: var(--orange);
  border-radius: 50%; display: inline-flex; align-items: center;
  justify-content: center; color: #fff; font-weight: 700; flex: none;
}
header.k-head h1 { font-size: 1.1rem; font-weight: 400; margin: 0; }
main { padding: 1.5rem 2rem 3rem; max-width: 1200px; margin: 0 auto; }
h2 { font-weight: 300; font-size: 1.6rem; margin: 0 0 0.25rem; }
.k-sub { color: var(--muted); margin: 0 0 1.5rem; font-size: 0.9rem; }
nav.k-tabs { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0 1.5rem; }
nav.k-tabs a {
  text-decoration: none; color: var(--ink); padding: 0.4rem 0.9rem;
  border: 1px solid #cdcdcd; border-radius: 2px; font-size: 0.9rem;
}
nav.k-tabs a.is-active { background: var(--orange); color: #fff; border-color: var(--orange); }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
thead th {
  text-align: left; border-bottom: 2px solid var(--orange);
  padding: 0.5rem 0.6rem; font-weight: 500;
}
tbody td { padding: 0.5rem 0.6rem; border-bottom: 1px solid #e5e5e5; vertical-align: top; }
tbody tr:hover { background: #faf7f8; }
td.k-id a { color: var(--orange); text-decoration: none; font-variant-numeric: tabular-nums; }
td.k-id a:hover { text-decoration: underline; }
.k-targets { color: var(--muted); font-size: 0.82rem; }
.k-empty { color: var(--muted); padding: 2rem 0; }
.k-cards { display: flex; flex-wrap: wrap; gap: 1rem; margin: 1.5rem 0; }
.k-card {
  flex: 1 1 10rem; border: 1px solid #cdcdcd; border-radius: 2px;
  padding: 1rem 1.25rem; text-decoration: none; color: var(--ink);
}
.k-card:hover { border-color: var(--orange); }
.k-card .k-count { font-size: 2rem; font-weight: 300; color: var(--orange); display: block; }
.k-card .k-label { color: var(--muted); font-size: 0.9rem; }
footer { color: var(--muted); font-size: 0.8rem; padding: 1.5rem 2rem; border-top: 1px solid #e5e5e5; }
""".strip()

LP_BUG_URL = "https://bugs.launchpad.net/bugs/"


def _head(title):
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{escape(title)}</title>\n"
        f"<style>\n{PAGE_CSS}\n</style>\n</head>\n<body>\n"
        "<header class=\"k-head\"><span class=\"k-logo\">U</span>"
        "<h1>Ubuntu kernel &mdash; newly opened bugs</h1></header>\n"
    )


def _tabs(active):
    items = ['<a href="index.html"%s>Overview</a>' % (' class="is-active"' if active == "index" else "")]
    for slug, label, _ in WINDOWS:
        cls = ' class="is-active"' if active == slug else ""
        items.append(f'<a href="{slug}.html"{cls}>{escape(label.replace("last ", ""))}</a>')
    return '<nav class="k-tabs">\n  ' + "\n  ".join(items) + "\n</nav>\n"


def _footer(now):
    return (
        "</main>\n<footer>Generated "
        f"{escape(now.strftime('%Y-%m-%d %H:%M UTC'))} &middot; "
        "data from <a href=\"https://bugs.launchpad.net/\">Launchpad</a></footer>\n"
        "</body>\n</html>\n"
    )


def _bug_in_window(record, now, days):
    cutoff = now - timedelta(days=days)
    hits = [parse_iso(t["date_created"]) for t in record["targets"]]
    hits = [h for h in hits if h >= cutoff]
    if not hits:
        return None
    return max(hits)


def _window_bugs(bugs, now, days):
    rows = []
    for record in bugs:
        newest = _bug_in_window(record, now, days)
        if newest is not None:
            rows.append((newest, record))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def render(store, now, output_dir):
    bugs = list(store.all_bugs())
    counts = {}

    for slug, label, days in WINDOWS:
        rows = _window_bugs(bugs, now, days)
        counts[slug] = len(rows)
        _write_window_page(os.path.join(output_dir, f"{slug}.html"), slug, label, rows, now)

    _write_index(os.path.join(output_dir, "index.html"), counts, now)


def _write_window_page(path, slug, label, rows, now):
    title = f"Kernel bugs opened in the {label}"
    out = [_head(title), "<main>\n", f"<h2>Bugs opened in the {escape(label)}</h2>\n"]
    out.append(f'<p class="k-sub">{len(rows)} bug(s) &middot; a bug raised against '
               "several kernel targets is listed once.</p>\n")
    out.append(_tabs(slug))

    if not rows:
        out.append('<p class="k-empty">No bugs opened in this window.</p>\n')
    else:
        out.append("<table>\n<thead>\n<tr>"
                   "<th>Bug</th><th>Title</th><th>Series</th>"
                   "<th>Importance</th><th>Status</th><th>Assignee</th><th>Targets</th>"
                   "</tr>\n</thead>\n<tbody>\n")
        for _, record in rows:
            targets = ", ".join(escape(t["name"]) for t in record["targets"])
            out.append(
                "<tr>"
                f'<td class="k-id"><a href="{LP_BUG_URL}{record["id"]}">#{record["id"]}</a></td>'
                f'<td><a href="{LP_BUG_URL}{record["id"]}" style="color:inherit;text-decoration:none">'
                f'{escape(record.get("title", ""))}</a></td>'
                f'<td>{escape(record.get("series") or "&mdash;")}</td>'
                f'<td>{escape(record.get("importance", ""))}</td>'
                f'<td>{escape(record.get("status", ""))}</td>'
                f'<td>{escape(record.get("assignee", ""))}</td>'
                f'<td class="k-targets">{targets}</td>'
                "</tr>\n"
            )
        out.append("</tbody>\n</table>\n")

    out.append(_footer(now))
    with open(path, "w") as fh:
        fh.write("".join(out))


def _write_index(path, counts, now):
    out = [_head("Ubuntu kernel newly opened bugs"), "<main>\n",
           "<h2>Newly opened kernel bugs</h2>\n",
           '<p class="k-sub">Bugs filed against Ubuntu kernel targets on Launchpad, '
           "bucketed by how recently they were opened.</p>\n"]
    out.append(_tabs("index"))
    out.append('<div class="k-cards">\n')
    for slug, label, _ in WINDOWS:
        out.append(
            f'<a class="k-card" href="{slug}.html">'
            f'<span class="k-count">{counts.get(slug, 0)}</span>'
            f'<span class="k-label">{escape(label)}</span></a>\n'
        )
    out.append("</div>\n")
    out.append(_footer(now))
    with open(path, "w") as fh:
        fh.write("".join(out))


def main():
    parser = ArgumentParser(
        description="Scan Launchpad for newly opened Ubuntu kernel bugs and render HTML reports.",
        formatter_class=RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output", default=".",
                        help="Root directory for state (bugs/, last-run.json) and the rendered HTML.")
    parser.add_argument("--state-dir",
                        help="Override the state directory (defaults to --output).")
    parser.add_argument("--credentials-file",
                        default=os.path.expanduser(
                            os.path.join("~/.config", "24hrs-of-bugs", "credentials-production")),
                        help="Launchpad credentials file. Required for tagging bugs.")
    parser.add_argument("--no-tag", action="store_true", help="Do not tag bugs on Launchpad.")
    parser.add_argument("--no-scan", action="store_true",
                        help="Skip the Launchpad scan and only re-render from the existing store.")
    parser.add_argument("--seed-days", type=int, default=RETENTION_DAYS,
                        help="History to ingest on the first run (no last-run stamp).")
    parser.add_argument("--fudge-minutes", type=int, default=FUDGE_MINUTES,
                        help="Overlap subtracted from the last-run timestamp on incremental runs.")
    parser.add_argument("--datetime",
                        help="Treat this UTC 'YYYY-MM-DD HH:MM' as 'now' (for testing).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.datetime:
        args.now = datetime.strptime(args.datetime + ":00+0000", "%Y-%m-%d %H:%M:%S%z")
    else:
        args.now = datetime.now(timezone.utc)
    log.info("II: Now: %s", iso(args.now))

    output_dir = os.path.abspath(args.output)
    state_dir = os.path.abspath(args.state_dir) if args.state_dir else output_dir
    os.makedirs(output_dir, exist_ok=True)
    store = Store(state_dir)

    if not args.no_scan:
        Scanner(args, store).run()

    render(store, args.now, output_dir)
    log.info("II: Finished: %s", iso(datetime.now(timezone.utc)))


if __name__ == "__main__":
    main()
