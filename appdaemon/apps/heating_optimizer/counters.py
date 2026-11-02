"""Persistent per-plug switch-on counters, keyed by device ID (MAC) so renaming a plug keeps its count."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


class SwitchOnCounters:
    """Counts off→on transitions per plug and keeps them in a small JSON file, so they survive restarts.

    If the file cannot be read or written, counting continues in memory and the problem is logged."""

    def __init__(self, path: Path, logger: logging.Logger):
        self.path = path
        self._log = logger
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {
                str(k): {"count": int(v["count"]), "since": v.get("since"), "last_on": v.get("last_on")}
                for k, v in raw.items()
            }
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self._log.error("Cannot read switch-on counters from %s, starting from 0: %s", self.path, exc)
            return {}

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)  # atomic: a crash never leaves a half-written file
        except OSError as exc:
            self._log.warning("Cannot save switch-on counters to %s: %s", self.path, exc)

    def increment(self, device_id: str, now: datetime) -> int:
        with self._lock:
            entry = self._data.setdefault(device_id, {"count": 0, "since": now.isoformat(), "last_on": None})
            entry["count"] += 1
            entry["last_on"] = now.isoformat()
            self._save()
            return entry["count"]

    def get(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._data.get(device_id) or {"count": 0, "since": None, "last_on": None}
            return dict(entry)
