import importlib.util
import os
from datetime import datetime, timedelta, timezone

import pytest

# Load the hyphenated script as a module.
_SPEC = importlib.util.spec_from_file_location(
    "report", os.path.join(os.path.dirname(__file__), "..", "24hrs-of-bugs.py")
)
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)

NOW = datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc)


def _target(name, days_ago):
    return {
        "name": name,
        "type": "package",
        "date_created": report.iso(NOW - timedelta(days=days_ago)),
    }


def _bug(bug_id, *targets):
    return {
        "id": bug_id,
        "title": f"bug {bug_id}",
        "series": "noble",
        "importance": "Medium",
        "status": "New",
        "assignee": "unassigned",
        "targets": list(targets),
    }


def test_iso_roundtrip():
    assert report.parse_iso(report.iso(NOW)) == NOW


def test_window_membership_uses_newest_target():
    # Bug opened long ago but a target task was added 2 hours ago.
    record = _bug(1, _target("linux", 40), _target("linux-aws", 0))
    assert report._bug_in_window(record, NOW, 1) is not None  # 24h
    assert report._bug_in_window(record, NOW, 90) is not None


def test_window_membership_excludes_old():
    record = _bug(2, _target("linux", 40))
    assert report._bug_in_window(record, NOW, 1) is None
    assert report._bug_in_window(record, NOW, 7) is None
    assert report._bug_in_window(record, NOW, 30) is None
    assert report._bug_in_window(record, NOW, 90) is not None


def test_window_bugs_sorted_newest_first():
    bugs = [
        _bug(1, _target("linux", 5)),
        _bug(2, _target("linux", 1)),
        _bug(3, _target("linux", 3)),
    ]
    rows = report._window_bugs(bugs, NOW, 7)
    assert [r[1]["id"] for r in rows] == [2, 3, 1]


def test_cumulative_windows():
    bugs = [
        _bug(1, _target("linux", 0)),
        _bug(2, _target("linux", 5)),
        _bug(3, _target("linux", 20)),
        _bug(4, _target("linux", 60)),
    ]
    assert len(report._window_bugs(bugs, NOW, 1)) == 1
    assert len(report._window_bugs(bugs, NOW, 7)) == 2
    assert len(report._window_bugs(bugs, NOW, 30)) == 3
    assert len(report._window_bugs(bugs, NOW, 90)) == 4


def test_store_upsert_and_prune(tmp_path):
    store = report.Store(str(tmp_path))
    assert store.read_last_run() is None

    store.save_bug(_bug(10, _target("linux", 0)))
    store.save_bug(_bug(11, _target("linux", 100)))

    loaded = store.load_bug(10)
    assert loaded["title"] == "bug 10"

    # Emulate the pruner.
    cutoff = NOW - timedelta(days=report.RETENTION_DAYS)
    for record in list(store.all_bugs()):
        newest = max(report.parse_iso(t["date_created"]) for t in record["targets"])
        if newest < cutoff:
            store.remove_bug(record["id"])

    assert store.load_bug(10) is not None
    assert store.load_bug(11) is None


def test_last_run_roundtrip(tmp_path):
    store = report.Store(str(tmp_path))
    store.write_last_run(NOW)
    assert store.read_last_run() == NOW


def test_retry_gives_up(monkeypatch):
    monkeypatch.setattr(report.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ConnectionError("flaky")

    with pytest.raises(report.RetryExhausted):
        report.retry("boom", boom)
    assert calls["n"] == report.RETRY_ATTEMPTS


def test_retry_succeeds_after_failures(monkeypatch):
    monkeypatch.setattr(report.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flaky")
        return "ok"

    assert report.retry("flaky", flaky) == "ok"
    assert calls["n"] == 3


def test_render_from_store(tmp_path):
    store = report.Store(str(tmp_path))
    store.save_bug(_bug(10, _target("linux", 0)))
    store.save_bug(_bug(11, _target("linux", 20)))
    report.render(store, NOW, str(tmp_path))

    for slug, _, _ in report.WINDOWS:
        assert os.path.exists(os.path.join(tmp_path, f"{slug}.html"))
    index = open(os.path.join(tmp_path, "index.html")).read()
    assert "Newly opened kernel bugs" in index

    day = open(os.path.join(tmp_path, "24-hours.html")).read()
    assert "/bugs/10" in day
    assert "/bugs/11" not in day
