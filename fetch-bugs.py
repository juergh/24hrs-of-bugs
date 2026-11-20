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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError as UrllibHTTPError
from urllib.error import URLError
from urllib.parse import urlparse

from launchpadlib.launchpad import Launchpad

try:
    from lazr.restfulclient.errors import HTTPError as LazrHTTPError
    from lazr.restfulclient.errors import ServerError as LazrServerError
except ImportError:  # pragma: no cover - lazr always ships with launchpadlib
    LazrHTTPError = ()
    LazrServerError = ()

from cktools_data import KernelTargets
from common import CLOSED_STATUSES, FUDGE_MINUTES, RETENTION_DAYS, Store, iso, parse_iso

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
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    URLError,
    socket.timeout,
    socket.error,
    ConnectionError,
)
if LazrServerError:
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (LazrServerError,)


class RetryExhausted(Exception):
    """Raised when a Launchpad operation keeps failing after all retries."""


def _is_retryable(exc: BaseException) -> bool:
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


def _retry_after(exc: BaseException) -> float | None:
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


def retry[T](description: str, func: Callable[[], T]) -> T:
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
            log.warning(
                "WW: %s failed (attempt %d/%d): %s; retrying in %.1fs",
                description,
                attempt,
                RETRY_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
    raise RetryExhausted(f"{description}: failed after {RETRY_ATTEMPTS} attempts")


def task_target(target_link: str) -> tuple[str, str, str] | None:
    """Extract (name, type, series) from a bug task's target_link URL.

    Distribution task: .../ubuntu/+source/<pkg>          -> (<pkg>, "package", "")
    Series task:       .../ubuntu/<series>/+source/<pkg> -> (<pkg>, "package", <series>)
    Project task:      .../<project>                     -> (<project>, "project", "")

    Returns None for source package tasks on non-Ubuntu distributions.
    """
    parts = urlparse(target_link).path.strip("/").split("/")
    if "+source" in parts:
        i = parts.index("+source")
        if parts[i - 1] == "ubuntu":
            return parts[i + 1], "package", ""
        if i >= 2 and parts[i - 2] == "ubuntu":
            return parts[i + 1], "package", parts[i - 1]
        return None
    return parts[-1], "project", ""


def merge_targets(old_targets: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge freshly scanned tasks with previously stored targets.

    Previously stored targets whose task no longer exists on Launchpad (the
    bug was retargeted or the task deleted) are preserved with the synthetic
    status "Removed" as an audit trail; a task that reappears on Launchpad is
    revived with its live data.
    """
    current_keys = {(t["name"], t["type"], t["series"]) for t in current}
    merged = list(current)
    for old in old_targets:
        key = (old["name"], old["type"], old.get("series", ""))
        if key in current_keys:
            continue
        merged.append(
            {
                "name": old["name"],
                "type": old["type"],
                "series": old.get("series", ""),
                "status": "Removed",
                "importance": old.get("importance", "Undecided"),
                "assignee": old.get("assignee", "unassigned"),
                "date_created": old["date_created"],
            }
        )
    merged.sort(key=lambda t: (t["name"], t["type"], t["series"]))
    return merged


class Scanner:
    """Drives the Launchpad scan and store upsert."""

    def __init__(self, args: Any, store: Store) -> None:
        self.args = args
        self.store = store
        self.now = args.now

        self.can_tag = False
        if args.credentials_file and os.path.exists(args.credentials_file):
            log.info("II: Launchpad login with credentials")
            self.lp = Launchpad.login_with(
                "24hrs-of-bugs",
                "production",
                version="devel",
                credentials_file=args.credentials_file,
            )
            self.can_tag = not args.no_tag
        else:
            log.info("II: Launchpad login anonymously")
            self.lp = Launchpad.login_anonymously("24hrs-of-bugs", "production", ".shadow-cache", version="devel")
        self.distro = self.lp.distributions["ubuntu"]

        self._targets = KernelTargets().all
        self._monitored_names = {
            "package": {t.name for t in self._targets if t.type == "package"},
            "project": {t.name for t in self._targets if t.type == "project"},
        }

        # Per-run cache of bug.tags keyed by bug id, and the set of bugs
        # already reconciled this run, so a bug hit via several targets is
        # only dereferenced and processed once.
        self._tags_cache: dict[int, list[str]] = {}
        self._processed_bugs: set[int] = set()
        self.failures = 0

    def _entry_for(self, name: str, type_: str) -> Any:
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

    def _bug_tags(self, bug: Any) -> list[str]:
        bug_id = bug.id
        if bug_id not in self._tags_cache:
            self._tags_cache[bug_id] = retry(f"bug {bug_id} tags", lambda: list(bug.tags))
        return self._tags_cache[bug_id]

    @staticmethod
    def _duplicate_of(bug: Any) -> int | None:
        """The id of the bug this one duplicates, or None."""
        link = bug.duplicate_of_link
        if not link:
            return None
        try:
            return int(link.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            return None

    def _monitored_tasks(self, bug: Any) -> list[dict[str, Any]]:
        """The bug's current tasks on monitored targets, from bug.bug_tasks."""
        tasks = retry(f"bug {bug.id} tasks", lambda: list(bug.bug_tasks))
        monitored = []
        for task in tasks:
            target = task_target(task.target_link)
            if target is None:
                continue
            name, type_, series = target
            if name not in self._monitored_names[type_]:
                continue
            try:
                assignee = task.assignee.display_name
            except AttributeError:
                assignee = "unassigned"
            except ValueError:
                assignee = "unknown"
            monitored.append(
                {
                    "name": name,
                    "type": type_,
                    "series": series,
                    "status": task.status,
                    "importance": task.importance,
                    "assignee": assignee,
                    "date_created": iso(task.date_created.astimezone(UTC)),
                }
            )
        return monitored

    def scan_target(self, name: str, type_: str, since: datetime) -> None:
        """Query one target and reconcile every in-window bug into the store."""
        entry = retry(f"lookup {type_} {name}", lambda: self._entry_for(name, type_))
        if entry is None:
            return

        tasks = retry(
            f"searchTasks {type_} {name}",
            lambda: entry.searchTasks(status=SEARCH_STATUS, modified_since=since, omit_duplicates=False),
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
            if bug.id in self._processed_bugs:
                continue
            self._processed_bugs.add(bug.id)

            tags = self._bug_tags(bug)
            if "kernel-release-tracking-bug" in tags:
                continue

            self._upsert(bug, tags)

    def _upsert(self, bug: Any, tags: list[str]) -> None:
        """Reconcile one bug record with its current Launchpad tasks.

        The full task list (bug.bug_tasks) is re-examined on every sighting
        so the record always reflects Launchpad reality: retargeted or
        deleted tasks are preserved with the synthetic status "Removed".
        """
        bug_id = bug.id
        record = self.store.load_bug(bug_id) or {"id": bug_id, "targets": []}

        targets = merge_targets(record["targets"], self._monitored_tasks(bug))
        if not targets:
            # No monitored tasks and nothing worth preserving.
            self.store.remove_bug(bug_id)
            log.info("II: Removed LP: #%s (no monitored tasks) -- %s", bug_id, bug.title)
            return

        # Ignore (and drop) bugs whose newest task predates the retention
        # window; the pruner would remove them again immediately.
        newest = max(parse_iso(t["date_created"]) for t in targets)
        if newest < self.now - timedelta(days=RETENTION_DAYS):
            self.store.remove_bug(bug_id)
            return

        record["title"] = bug.title
        record["duplicate_of"] = self._duplicate_of(bug)
        record["last_seen"] = iso(self.now)
        record["targets"] = targets
        # Drop legacy top-level fields superseded by the per-target data.
        for key in ("series", "importance", "status", "assignee"):
            record.pop(key, None)

        self.store.save_bug(record)
        log.info("II: Upserted LP: #%s -- %s", bug_id, bug.title)

        if self._should_tag(record):
            self._tag(bug, tags, record)

    def _should_tag(self, record: dict[str, Any]) -> bool:
        if not self.can_tag or record["duplicate_of"] is not None:
            return False
        cutoff = self.now - timedelta(days=1)
        return any(
            t["status"] not in CLOSED_STATUSES and parse_iso(t["date_created"]) >= cutoff for t in record["targets"]
        )

    def _tag(self, bug: Any, tags: list[str], record: dict[str, Any]) -> None:
        """Apply the daily-bug tags: kernel-daily-bug, plus
        linux-firmware-daily-bug for firmware targets. Best-effort: failures
        are logged, not fatal.
        """
        new_tags = []
        if "kernel-daily-bug" not in tags:
            new_tags.append("kernel-daily-bug")
        has_firmware = any(t["name"].startswith("linux-firmware") for t in record["targets"])
        if has_firmware and "linux-firmware-daily-bug" not in tags:
            new_tags.append("linux-firmware-daily-bug")
        if not new_tags:
            return
        log.info("II: Tagging LP: #%s with %s", bug.id, new_tags)

        def _save() -> None:
            bug.tags = tags + new_tags
            bug.lp_save()

        try:
            retry(f"tag bug {bug.id}", _save)
            self._tags_cache[bug.id] = tags + new_tags
        except Exception as exc:  # noqa: BLE001 - tagging is best-effort
            log.warning("WW: Failed to tag LP: #%s: %s", bug.id, exc)

    def run(self) -> None:
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

        targets = sorted({(t.name, t.type) for t in self._targets})
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

    def prune(self) -> None:
        cutoff = self.now - timedelta(days=RETENTION_DAYS)
        removed = 0
        for record in self.store.all_bugs():
            newest = max(parse_iso(t["date_created"]) for t in record["targets"])
            if newest < cutoff:
                self.store.remove_bug(record["id"])
                removed += 1
        if removed:
            log.info("II: Pruned %d bug(s) older than %d days", removed, RETENTION_DAYS)


def main() -> None:
    parser = ArgumentParser(
        description="Scan Launchpad for newly opened Ubuntu kernel bugs into the JSON store.",
        formatter_class=RawDescriptionHelpFormatter,
    )
    parser.add_argument("--state-dir", default=".", help="Directory for the store (bugs/, last-run.json).")
    parser.add_argument(
        "--credentials-file",
        default=os.path.expanduser(os.path.join("~/.config", "24hrs-of-bugs", "credentials-production")),
        help="Launchpad credentials file. Required for tagging bugs.",
    )
    parser.add_argument("--no-tag", action="store_true", help="Do not tag bugs on Launchpad.")
    parser.add_argument(
        "--seed-days",
        type=int,
        default=RETENTION_DAYS,
        help="History to ingest on the first run (no last-run stamp).",
    )
    parser.add_argument(
        "--fudge-minutes",
        type=int,
        default=FUDGE_MINUTES,
        help="Overlap subtracted from the last-run timestamp on incremental runs.",
    )
    parser.add_argument("--datetime", help="Treat this UTC 'YYYY-MM-DD HH:MM' as 'now' (for testing).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.datetime:
        args.now = datetime.strptime(args.datetime + ":00+0000", "%Y-%m-%d %H:%M:%S%z")
    else:
        args.now = datetime.now(UTC)
    log.info("II: Now: %s", iso(args.now))

    store = Store(os.path.abspath(args.state_dir))
    Scanner(args, store).run()
    log.info("II: Finished: %s", iso(datetime.now(UTC)))


if __name__ == "__main__":
    main()
