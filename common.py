#
# Shared helpers for the 24hrs-of-bugs fetcher and renderer: the on-disk store,
# timestamp helpers and the reporting window definitions.
#

import json
import os
from datetime import timezone
from datetime import datetime

# Reporting windows (slug, human label, number of days).
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


def iso(dt):
    """Serialise a datetime as a UTC ISO-8601 string."""
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    """Parse an ISO-8601 string into an aware UTC datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# On-disk bug record schema — the contract between fetch-bugs.py and
# render-html.py. One JSON file per bug, keyed by Launchpad bug id:
#
#     {
#       "id": 1234567,
#       "title": "...", "series": "noble",
#       "importance": "Medium", "status": "New", "assignee": "...",
#       "last_seen": "<utc iso>",
#       "targets": [
#         {"name": "linux", "type": "package", "date_created": "<utc iso>"},
#         ...
#       ]
#     }
#
# "targets" records every kernel target a task has been filed against, each
# with its own task creation date; a bug is stored once even when raised
# against several targets.
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
        """Write the last-run stamp, atomically (tmp file + rename)."""
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
        """Persist a bug record, atomically (tmp file + rename)."""
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
                continue

    def remove_bug(self, bug_id):
        try:
            os.remove(self._bug_path(bug_id))
        except FileNotFoundError:
            pass
