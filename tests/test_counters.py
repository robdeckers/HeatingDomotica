import json
import logging
from datetime import UTC, datetime

from heating_optimizer.counters import SwitchOnCounters

LOG = logging.getLogger("test")
T1 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)


def test_counts_per_device_and_persists(tmp_path):
    path = tmp_path / "switch_on_counts.json"
    counters = SwitchOnCounters(path, LOG)
    assert counters.get("aa")["count"] == 0
    assert counters.increment("aa", T1) == 1
    assert counters.increment("aa", T2) == 2
    assert counters.increment("bb", T2) == 1
    reloaded = SwitchOnCounters(path, LOG)
    assert reloaded.get("aa") == {"count": 2, "since": T1.isoformat(), "last_on": T2.isoformat()}
    assert reloaded.get("bb")["count"] == 1
    assert not path.with_name(path.name + ".tmp").exists()


def test_corrupt_file_starts_from_zero(tmp_path):
    path = tmp_path / "switch_on_counts.json"
    path.write_text("{not json")
    counters = SwitchOnCounters(path, LOG)
    assert counters.get("aa")["count"] == 0
    assert counters.increment("aa", T1) == 1
    assert json.loads(path.read_text())["aa"]["count"] == 1


def test_unwritable_location_keeps_counting_in_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    counters = SwitchOnCounters(blocker / "sub" / "switch_on_counts.json", LOG)  # parent is a file
    assert counters.increment("aa", T1) == 1
    assert counters.increment("aa", T2) == 2
