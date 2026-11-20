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


def test_window_bugs_sorted_newest_first():
    bugs = [
        _bug(1, _target("linux", 5)),
        _bug(2, _target("linux", 1)),
        _bug(3, _target("linux", 3)),
    ]
    rows = render._window_bugs(bugs, NOW, 7)
    assert [r["id"] for r in rows] == [2, 3, 1]


def test_cumulative_windows():
    bugs = [
        _bug(1, _target("linux", 0)),
        _bug(2, _target("linux", 5)),
        _bug(3, _target("linux", 20)),
        _bug(4, _target("linux", 60)),
    ]
    assert len(render._window_bugs(bugs, NOW, 1)) == 1
    assert len(render._window_bugs(bugs, NOW, 7)) == 2
    assert len(render._window_bugs(bugs, NOW, 30)) == 3
    assert len(render._window_bugs(bugs, NOW, 90)) == 4


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


def test_brief_multiple_values():
    record = _bug(1, _target("linux", 1, status="New"), _target("linux-aws", 2, status="Confirmed"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "multiple"
    assert "New" in brief["status"]["tooltip"]
    assert "Confirmed" in brief["status"]["tooltip"]


def test_brief_ignores_inactive_targets():
    # A removed/invalid task must not drag the brief status into "multiple".
    record = _bug(1, _target("linux", 1, status="Removed"), _target("linux-aws", 2, status="In Progress"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "In Progress"

    record = _bug(1, _target("linux", 1, status="Invalid"), _target("linux-aws", 2, status="New"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "New"


def test_brief_all_closed_falls_back_to_all_targets():
    record = _bug(1, _target("linux", 1, status="Invalid"))
    brief = render._brief(record)
    assert brief["status"]["value"] == "Invalid"


def test_brief_duplicate_is_status():
    record = _bug(1, _target("linux", 1, status="New"), duplicate_of=42)
    brief = render._brief(record)
    assert brief["status"] == "Duplicate"


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

    for slug, _, _ in common.WINDOWS:
        assert os.path.exists(os.path.join(tmp_path, f"{slug}.html"))
    index = open(os.path.join(tmp_path, "index.html")).read()
    assert "Newly opened kernel bugs" in index

    day = open(os.path.join(tmp_path, "24-hours.html")).read()
    assert "/bugs/10" in day
    assert "/bugs/11" not in day
    assert "<td>&mdash;</td>" in day
    assert "&amp;mdash;" not in day
    assert 'data-series=""' in day
    assert 'data-targets="linux"' in day
    assert 'id="k-pkg-search"' in day
    assert 'id="k-filter-clear"' in day
    assert 'data-series="none"' in day
    assert 'id="k-view-switch"' in day
    assert 'id="k-bugs-table"' in day
    assert "applyFilters" in day


def test_render_multi_target_details(tmp_path):
    store = common.Store(str(tmp_path))
    record = _bug(20, _target("linux", 1, status="Removed"), _target("linux-aws", 0, status="In Progress"))
    store.save_bug(record)
    render.render(store, NOW, str(tmp_path))

    day = open(os.path.join(tmp_path, "24-hours.html")).read()
    # Only one active task: the brief status must not be "multiple".
    assert 'class="k-multiple"' not in day
    # The removed target is shown struck through.
    assert 'class="k-target-inactive"' in day
    # Verbose details: toggle, detail row and per-task data.
    assert "k-toggle" in day
    assert 'class="k-details"' in day
    assert "In Progress" in day
    assert "Removed" in day


def test_render_multiple_indicator(tmp_path):
    store = common.Store(str(tmp_path))
    record = _bug(21, _target("linux", 0, status="New"), _target("linux-aws", 0, status="Confirmed"))
    store.save_bug(record)
    render.render(store, NOW, str(tmp_path))

    day = open(os.path.join(tmp_path, "24-hours.html")).read()
    assert 'class="k-multiple"' in day
    assert ">multiple</span>" in day


def test_render_duplicate(tmp_path):
    store = common.Store(str(tmp_path))
    store.save_bug(_bug(22, _target("linux", 0), duplicate_of=99))
    render.render(store, NOW, str(tmp_path))

    day = open(os.path.join(tmp_path, "24-hours.html")).read()
    assert 'class="k-duplicate"' in day
    assert "/bugs/99" in day
