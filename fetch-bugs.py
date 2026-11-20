#!/usr/bin/env python3
#
# Scan Launchpad for newly opened bugs filed against Ubuntu kernel targets and
# maintain a persistent per-bug JSON store.
#
# This is the "fetcher" half of 24hrs-of-bugs; rendering the store into HTML is
# done separately by render-html.py. It is designed to run as a time-triggered
# Forgejo action: the JSON store lives in git and is updated incrementally on
# every run.

import logging
import os
import random
import socket
import time
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from datetime import datetime, timedelta, timezone
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

from common import FUDGE_MINUTES, RETENTION_DAYS, Store, iso, parse_iso

log = logging.getLogger("fetch-bugs")

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
        """Upsert one JSON record per bug (dedup by bug id).

        A bug raised against several kernel targets is stored once: append a
        new target task, or refresh its creation date if already known, and
        refresh title/series/importance/status/assignee on every sighting.
        """
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
        """Apply the daily-bug tags: kernel-daily-bug, plus
        linux-firmware-daily-bug for firmware targets. Best-effort: failures
        are logged, not fatal.
        """
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
        """Scan all kernel targets and update the store.

        Self-healing: the last-run stamp is advanced only when every target
        scanned successfully, so a partial failure is re-covered on the next
        run (the fudge factor absorbs the overlap).
        """
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


def main():
    parser = ArgumentParser(
        description="Scan Launchpad for newly opened Ubuntu kernel bugs into the JSON store.",
        formatter_class=RawDescriptionHelpFormatter,
    )
    parser.add_argument("--state-dir", default=".",
                        help="Directory for the store (bugs/, last-run.json).")
    parser.add_argument("--credentials-file",
                        default=os.path.expanduser(
                            os.path.join("~/.config", "24hrs-of-bugs", "credentials-production")),
                        help="Launchpad credentials file. Required for tagging bugs.")
    parser.add_argument("--no-tag", action="store_true", help="Do not tag bugs on Launchpad.")
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

    store = Store(os.path.abspath(args.state_dir))
    Scanner(args, store).run()
    log.info("II: Finished: %s", iso(datetime.now(timezone.utc)))


if __name__ == "__main__":
    main()
