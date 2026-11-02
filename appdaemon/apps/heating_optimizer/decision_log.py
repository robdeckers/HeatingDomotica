"""Decision log: one JSONL record and one CSV row per tick, both size-rotated."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CSV_COLUMNS = (
    "ts",
    "tick",
    "hour_change",
    "action",
    "reason_code",
    "elec_per_kwh_heat",
    "gas_per_kwh_heat",
    "elec_consumer_kwh",
    "gas_consumer_m3",
    "elec_applies_to",
    "gas_day",
    "margin_on",
    "margin_off",
    "devices",
    "duration_ms",
    "reason",
)


class _RotatingFile:
    """Append-only text file rotated by size (file → file.1 → … → file.N). Writes a header to each new file."""

    def __init__(self, path: Path, max_bytes: int, backups: int, header: str | None = None):
        self.path = path
        self._max_bytes = max_bytes
        self._backups = backups
        self._header = header

    def _rotate(self) -> None:
        for i in range(self._backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def rotate_if_header_changed(self) -> None:
        """Rotate an existing file whose header differs (columns changed), so new rows never sit under old headers."""
        if not self._header or not self.path.exists():
            return
        with self.path.open(encoding="utf-8", newline="") as handle:
            first = handle.readline()
        if first and first != self._header:
            self._rotate()

    def write(self, line: str) -> None:
        if self.path.exists() and self.path.stat().st_size + len(line) > self._max_bytes:
            self._rotate()
        new_file = not self.path.exists()
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            if new_file and self._header:
                handle.write(self._header)
            handle.write(line)


def _devices_summary(devices: list[Mapping[str, Any]]) -> str:
    parts = []
    for d in devices:
        mark = "ok" if d.get("verified") else "FAIL"
        extra = f" {d['skipped']}" if d.get("skipped") else ""
        parts.append(f"{d.get('name')}:{d.get('before')}>{d.get('after')} {mark}{extra}")
    return "; ".join(parts)


def csv_row(record: Mapping[str, Any]) -> dict[str, Any]:
    elec = record.get("elec") or {}
    gas = record.get("gas") or {}
    margins = record.get("margins") or {}
    return {
        "ts": record.get("ts"),
        "tick": record.get("tick"),
        "hour_change": record.get("hour_change"),
        "action": record.get("action"),
        "reason_code": record.get("reason_code"),
        "elec_per_kwh_heat": elec.get("per_kwh_heat"),
        "gas_per_kwh_heat": gas.get("per_kwh_heat"),
        "elec_consumer_kwh": elec.get("consumer_kwh"),
        "gas_consumer_m3": gas.get("consumer_m3"),
        "elec_applies_to": elec.get("applies_to"),
        "gas_day": gas.get("gas_day"),
        "margin_on": margins.get("on"),
        "margin_off": margins.get("off"),
        "devices": _devices_summary(record.get("devices") or []),
        "duration_ms": record.get("duration_ms"),
        "reason": record.get("reason"),
    }


def _csv_line(values: Mapping[str, Any] | None) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS if values is None else [values.get(c) for c in CSV_COLUMNS])
    return buffer.getvalue()


class DecisionLogger:
    def __init__(self, log_dir: str, rotate_mb: float, backups: int):
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        max_bytes = int(rotate_mb * 1024 * 1024)
        self._jsonl = _RotatingFile(directory / "decisions.jsonl", max_bytes, backups)
        self._csv = _RotatingFile(directory / "decisions.csv", max_bytes, backups, header=_csv_line(None))
        self._csv.rotate_if_header_changed()

    def write(self, record: Mapping[str, Any]) -> None:
        self._jsonl.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._csv.write(_csv_line(csv_row(record)))
