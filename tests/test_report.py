import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import common  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, "..", filename))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load("render_html", "render-html.py")
fetch = _load("fetch_bugs", "fetch-bugs.py")

NOW = datetime(2026, 11, 19, 1, 0, tzinfo=UTC)


def _target(name, days_ago, series="", status="New", importance="Medium", assignee="unassigned"):
    return {
        "name": name,
        "type": "package",
        "series": series,
        "status": status,
        "importance": importance,
        "assignee": assignee,
        "date_created": common.iso(NOW - timedelta(days=days_ago)),
    }


def _bug(bug_id, *targets, duplicate_of=None):
    return {
        "id": bug_id,
        "title": f"bug {bug_id}",
        "duplicate_of": duplicate_of,
        "targets": list(targets),
    }


def test_iso_roundtrip():
    assert common.parse_iso(common.iso(NOW)) == NOW


def test_window_membership_uses_newest_target():
    # Bug opened long ago but a target task was added 2 hours ago.
    record = _bug(1, _target("linux", 40), _target("linux-aws", 0))
    assert render._bug_in_window(record, NOW, 1) is not None  # 24h
    assert render._bug_in_window(record, NOW, 90) is not None


def test_window_membership_excludes_old():
    record = _bug(2, _target("linux", 40))
    assert render._bug_in_window(record, NOW, 1) is None
    assert render._bug_in_window(record, NOW, 7) is None
    assert render._bug_in_window(record, NOW, 30) is None
    assert render._bug_in_window(record, NOW, 90) is not None


def test_rows_sorted_newest_first():
    bugs = [
        _bug(1, _target("linux", 5)),
        _bug(2, _target("linux", 1)),
        _bug(3, _target("linux", 3)),
    ]
    rows = render._rows(bugs, NOW)
    assert [r["id"] for r in rows] == [2, 3, 1]


def test_rows_exclude_beyond_retention():
    bugs = [
        _bug(1, _target("linux", 60)),
        _bug(2, _target("linux", 100)),
    ]
    rows = render._rows(bugs, NOW)
    assert [r["id"] for r in rows] == [1]


def test_counts_cumulative_windows():
    bugs = [
        _bug(1, _target("linux", 0)),
        _bug(2, _target("linux", 5)),
        _bug(3, _target("linux", 20)),
        _bug(4, _target("linux", 60)),
    ]
    rows = render._rows(bugs, NOW)
    counts = render._counts(rows, NOW)
    assert counts["24-hours"] == 1
    assert counts["7-days"] == 2
    assert counts["30-days"] == 3
    assert counts["90-days"] == 4


def test_store_upsert_and_prune(tmp_path):
    store = common.Store(str(tmp_path))
    assert store.read_last_run() is None

    store.save_bug(_bug(10, _target("linux", 0)))
    store.save_bug(_bug(11, _target("linux", 100)))

    loaded = store.load_bug(10)
    assert loaded is not None
    assert loaded["title"] == "bug 10"

    # Emulate the pruner.
    cutoff = NOW - timedelta(days=common.RETENTION_DAYS)
    for record in list(store.all_bugs()):
        newest = max(common.parse_iso(t["date_created"]) for t in record["targets"])
        if newest < cutoff:
            store.remove_bug(record["id"])

    assert store.load_bug(10) is not None
    assert store.load_bug(11) is None


def test_last_run_roundtrip(tmp_path):
    store = common.Store(str(tmp_path))
    store.write_last_run(NOW)
    assert store.read_last_run() == NOW


def test_render_anchors_to_last_run(tmp_path):
    store = common.Store(str(tmp_path))
    # Bug opened 60 hours ago; the store was last scanned 48 hours ago, so
    # relative to the stamp the bug is 12 hours old (inside the 24h window),
    # while relative to the render wall-clock it is 60 hours old (outside).
    bug30 = _bug(
        30,
        {
            "name": "linux",
            "type": "package",
            "series": "",
            "status": "New",
            "importance": "Medium",
            "assignee": "unassigned",
            "date_created": common.iso(NOW - timedelta(hours=60)),
        },
    )
    store.save_bug(bug30)
    stamp = NOW - timedelta(hours=48)
    store.write_last_run(stamp)
    render.render(store, NOW, str(tmp_path))

    page = open(os.path.join(tmp_path, "index.html")).read()
    # The data anchor is the last-run stamp, not the render time...
    assert f'data-as-of="{common.iso(stamp)}"' in page
    assert f"data as of {stamp.strftime('%Y-%m-%d %H:%M UTC')}" in page
    # ...so window selection and preset counts are relative to the stamp.
    assert 'data-slug="24-hours" data-days="1">24 hours <span class="k-range-count">1</span>' in page
    # The footer still records the actual render time.
    assert f"Generated {NOW.strftime('%Y-%m-%d %H:%M UTC')}" in page


def test_retry_gives_up(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ConnectionError("flaky")

    with pytest.raises(fetch.RetryExhausted):
        fetch.retry("boom", boom)
    assert calls["n"] == fetch.RETRY_ATTEMPTS


def test_retry_succeeds_after_failures(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flaky")
        return "ok"

    assert fetch.retry("flaky", flaky) == "ok"
    assert calls["n"] == 3


def test_task_target_parsing():
    # Unversioned distribution task.
    assert fetch.task_target("https://api.launchpad.net/devel/ubuntu/+source/linux") == ("linux", "package", "")
    # Series (nomination) task.
    assert fetch.task_target("https://api.launchpad.net/devel/ubuntu/noble/+source/linux") == (
        "linux",
        "package",
        "noble",
    )
    # Project task.
    assert fetch.task_target("https://api.launchpad.net/devel/canonical-kernel-snaps") == (
        "canonical-kernel-snaps",
        "project",
        "",
    )
    # Source package task on a non-Ubuntu distribution is not monitored.
    assert fetch.task_target("https://api.launchpad.net/devel/debian/+source/linux") is None


def test_merge_targets_marks_missing_as_removed():
    old = [_target("linux", 5)]
    current = [_target("linux-aws", 1, status="Confirmed")]
    merged = fetch.merge_targets(old, current)
    by_name = {t["name"]: t for t in merged}
    assert by_name["linux"]["status"] == "Removed"
    # The original creation date is preserved for the audit trail.
    assert by_name["linux"]["date_created"] == old[0]["date_created"]
    assert by_name["linux-aws"]["status"] == "Confirmed"


def test_merge_targets_revives_reappearing_task():
    old = [_target("linux", 5, status="Removed"), _target("linux-aws", 3)]
    current = [_target("linux", 5, status="Confirmed")]
    merged = fetch.merge_targets(old, current)
    by_name = {t["name"]: t for t in merged}
    assert by_name["linux"]["status"] == "Confirmed"
    assert by_name["linux-aws"]["status"] == "Removed"


def test_merge_targets_distinguishes_series():
    old = [_target("linux", 5, series="jammy")]
    current = [_target("linux", 5, series="noble")]
    merged = fetch.merge_targets(old, current)
    assert len(merged) == 2
    by_series = {t["series"]: t for t in merged}
    assert by_series["jammy"]["status"] == "Removed"
    assert by_series["noble"]["status"] == "New"


def test_duplicate_of_parsing():
    class Dup:
        duplicate_of_link = "https://api.launchpad.net/devel/bugs/2173618"

    class NoDup:
        duplicate_of_link = None

    assert fetch.Scanner._duplicate_of(Dup()) == 2173618
    assert fetch.Scanner._duplicate_of(NoDup()) is None


def test_brief_shared_values():
    record = _bug(1, _target("linux", 1, status="New"), _target("linux-aws", 2, status="New"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "New"
    assert brief["importance"]["value"] == "Medium"


def test_brief_status_open_when_active_tasks_differ():
    record = _bug(1, _target("linux", 1, status="New"), _target("linux-aws", 2, status="Confirmed"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "Open"
    assert "New" in brief["status"]["tooltip"]
    assert "Confirmed" in brief["status"]["tooltip"]


def test_brief_status_closed_when_done_tasks_differ():
    record = _bug(1, _target("linux", 1, status="Fix Released"), _target("linux-aws", 2, status="Invalid"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "Closed"
    assert "Fix Released" in brief["status"]["tooltip"]
    assert "Invalid" in brief["status"]["tooltip"]


def test_brief_other_fields_still_multiple():
    record = _bug(
        1,
        _target("linux", 1, status="New", importance="High"),
        _target("linux-aws", 2, status="New", importance="Low"),
    )
    brief = render._brief(record)
    assert brief["importance"]["value"] == "multiple"
    assert "High" in brief["importance"]["tooltip"]
    assert "Low" in brief["importance"]["tooltip"]


def test_brief_ignores_inactive_targets():
    # A removed/invalid task must not drag the brief status into "multiple".
    record = _bug(1, _target("linux", 1, status="Removed"), _target("linux-aws", 2, status="In Progress"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "In Progress"

    record = _bug(1, _target("linux", 1, status="Invalid"), _target("linux-aws", 2, status="New"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "New"


def test_brief_ignores_fix_released_targets():
    # "Fix Released" is done work: it must not drag the brief status into
    # "multiple" either.
    record = _bug(1, _target("linux", 1, status="Fix Released"), _target("linux-aws", 2, status="In Progress"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "In Progress"

    # ...but a fully fixed bug still reports Fix Released (fallback pool).
    record = _bug(1, _target("linux", 1, status="Fix Released"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "Fix Released"


def test_brief_all_closed_falls_back_to_all_targets():
    record = _bug(1, _target("linux", 1, status="Invalid"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "Invalid"


def test_brief_duplicate_is_status():
    record = _bug(1, _target("linux", 1, status="New"), duplicate_of=42)
    brief = render._brief(record)
    assert brief["status"]["value"] == "Duplicate"


def test_brief_series():
    record = _bug(1, _target("linux", 1, series="noble"), _target("linux", 1, series="jammy"))
    brief = render._brief(record)
    assert brief["series"]["value"] == "multiple"
    assert "noble" in brief["series"]["tooltip"]
    assert "jammy" in brief["series"]["tooltip"]

    record = _bug(1, _target("linux", 1))
    brief = render._brief(record)
    assert brief["series"]["value"] == ""


def test_display_targets_flags_inactive():
    record = _bug(1, _target("linux", 1, status="Removed"), _target("linux-aws", 0, status="New"))
    targets = render._display_targets(record)
    by_name = {t["name"]: t for t in targets}
    assert by_name["linux"]["inactive"] is True
    assert by_name["linux-aws"]["inactive"] is False


def test_render_from_store(tmp_path):
    store = common.Store(str(tmp_path))
    store.save_bug(_bug(10, _target("linux", 0)))
    store.save_bug(_bug(11, _target("linux", 20)))
    render.render(store, NOW, str(tmp_path))

    # Only the single page is rendered; no per-window pages.
    assert os.path.exists(os.path.join(tmp_path, "index.html"))
    for slug, _, _ in common.WINDOWS:
        assert not os.path.exists(os.path.join(tmp_path, f"{slug}.html"))

    page = open(os.path.join(tmp_path, "index.html")).read()
    assert "Bugs opened against Ubuntu kernel targets" in page
    # Both bugs are on the single page (the date filter scopes the view).
    assert "/bugs/10" in page
    assert "/bugs/11" in page
    assert "<td>&mdash;</td>" in page
    assert "&amp;mdash;" not in page
    assert 'data-series=""' in page
    assert 'data-targets="linux"' in page
    assert "data-opened=" in page
    assert "data-active=" in page
    assert "data-as-of=" in page
    assert 'id="k-ranges"' in page
    assert 'data-slug="24-hours"' in page
    assert 'data-days="7"' in page
    assert "k-range-count" in page
    assert 'id="k-date-from"' in page
    assert 'id="k-date-to"' in page
    assert 'id="k-active-only"' in page
    assert 'id="k-pkg-search"' in page
    assert 'id="k-filter-clear"' in page
    assert 'data-series="none"' in page
    assert 'id="k-verbose-toggle"' in page
    assert 'id="k-bugs-table"' in page
    assert "applyFilters" in page


def test_render_multi_target_details(tmp_path):
    store = common.Store(str(tmp_path))
    record = _bug(20, _target("linux", 1, status="Removed"), _target("linux-aws", 0, status="In Progress"))
    store.save_bug(record)
    render.render(store, NOW, str(tmp_path))

    page = open(os.path.join(tmp_path, "index.html")).read()
    # Only one active task: the brief status must not be "multiple".
    assert 'class="k-multiple"' not in page
    # The removed target is shown struck through.
    assert 'class="k-target-inactive"' in page
    # Verbose details: toggle in its own column, per-task detail rows that
    # align with the outer table columns (no nested table, no colspan).
    assert 'class="k-toggle-cell"' in page
    assert "k-toggle" in page
    assert 'class="k-details' in page
    assert "table class=" not in page
    assert 'colspan="9"' not in page
    assert '<td class="k-opened" title="Opened (UTC)">' in page
    assert '<td class="k-targets">linux</td>' in page
    assert '<td class="k-targets">linux-aws</td>' in page
    assert "In Progress" in page
    assert "Removed" in page
    # The removed task's detail row is muted and filterable by Active only.
    assert 'class="k-details k-inactive" data-active="false"' in page
    assert 'class="k-details" data-active="true"' in page


def test_render_open_indicator(tmp_path):
    store = common.Store(str(tmp_path))
    record = _bug(21, _target("linux", 0, status="New"), _target("linux-aws", 0, status="Confirmed"))
    store.save_bug(record)
    render.render(store, NOW, str(tmp_path))

    page = open(os.path.join(tmp_path, "index.html")).read()
    assert '<span class="k-multiple" title="New, Confirmed">Open</span>' in page


def test_render_multiple_indicator(tmp_path):
    store = common.Store(str(tmp_path))
    record = _bug(
        24,
        _target("linux", 0, status="New", importance="High"),
        _target("linux-aws", 0, status="New", importance="Low"),
    )
    store.save_bug(record)
    render.render(store, NOW, str(tmp_path))

    page = open(os.path.join(tmp_path, "index.html")).read()
    assert '<span class="k-multiple" title="High, Low">multiple</span>' in page


def test_render_duplicate(tmp_path):
    store = common.Store(str(tmp_path))
    store.save_bug(_bug(22, _target("linux", 0), duplicate_of=99))
    render.render(store, NOW, str(tmp_path))

    page = open(os.path.join(tmp_path, "index.html")).read()
    assert 'class="k-duplicate"' in page
    assert "/bugs/99" in page
    # A duplicate has no active work: the row is muted and filterable.
    assert "k-inactive-row" in page
    assert 'data-active="false"' in page


def test_render_title_is_plain_truncating_text(tmp_path):
    store = common.Store(str(tmp_path))
    store.save_bug(_bug(23, _target("linux", 0)))
    render.render(store, NOW, str(tmp_path))

    page = open(os.path.join(tmp_path, "index.html")).read()
    # The title is plain text in a truncating cell with a hover tooltip,
    # not a link (row clicks expand the task details).
    assert '<td class="k-title" title="bug 23">bug 23</td>' in page
