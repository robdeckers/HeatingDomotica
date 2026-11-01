import csv
import json

from heating_optimizer.decision_log import CSV_COLUMNS, DecisionLogger

RECORD = {
    "ts": "2026-10-27T14:05:00+01:00",
    "tick": "aligned",
    "dry_run": True,
    "hour_change": False,
    "action": "ON",
    "reason_code": "ELEC_CHEAPER",
    "reason": "Electric heat, cheaper",
    "elec": {"consumer_kwh": 0.118, "per_kwh_heat": 0.118, "applies_to": "14:00-15:00"},
    "gas": {"consumer_m3": 1.31, "per_kwh_heat": 0.149, "gas_day": "2026-10-27"},
    "margins": {"on": 0.02, "off": 0.005},
    "devices": [{"name": "shelly_1", "before": "off", "after": "on", "verified": True, "skipped": None}],
    "duration_ms": 412,
}


def test_writes_jsonl_and_csv(tmp_path):
    log = DecisionLogger(str(tmp_path / "logs"), 5, 3)
    log.write(RECORD)
    log.write(RECORD)
    lines = (tmp_path / "logs" / "decisions.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["reason_code"] == "ELEC_CHEAPER"
    rows = list(csv.DictReader((tmp_path / "logs" / "decisions.csv").open()))
    assert len(rows) == 2
    assert tuple(rows[0]) == CSV_COLUMNS
    assert rows[0]["devices"] == "shelly_1:off>on ok"
    assert rows[0]["reason"] == "Electric heat, cheaper"


def test_rotation_keeps_csv_header(tmp_path):
    log = DecisionLogger(str(tmp_path), 0.001, 2)  # ~1 kB
    for _ in range(30):
        log.write(RECORD)
    assert (tmp_path / "decisions.csv.1").exists()
    assert (tmp_path / "decisions.csv.2").exists()
    assert not (tmp_path / "decisions.csv.3").exists()
    for name in ("decisions.csv", "decisions.csv.1"):
        assert (tmp_path / name).read_text().startswith("ts,tick,")
