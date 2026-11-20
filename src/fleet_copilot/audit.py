"""Append-only, hash-chained audit log."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq, self._last_hash = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if not last:
            return 0, GENESIS
        rec = json.loads(last)
        return int(rec["seq"]), rec["hash"]

    def record(self, event: str, *, tenant_id: str | None, session_id: str | None, actor: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            body = {
                "seq": self._seq + 1,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "event": event,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "actor": actor,
                "data": data or {},
                "prev_hash": self._last_hash,
            }
            body["hash"] = hashlib.sha256((self._last_hash + _canonical(body)).encode("utf-8")).hexdigest()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(_canonical(body) + "\n")
            self._seq = body["seq"]
            self._last_hash = body["hash"]
            return body

    def events(self, session_id: str | None = None, event: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if session_id and rec.get("session_id") != session_id:
                    continue
                if event and rec.get("event") != event:
                    continue
                out.append(rec)
        return out


def verify_chain(path: str | Path) -> tuple[bool, int, str]:
    """Returns (ok, records_checked, message)."""
    path = Path(path)
    if not path.exists():
        return True, 0, "no audit log yet"
    prev = GENESIS
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            rec = json.loads(line)
            claimed = rec.pop("hash", None)
            if rec.get("prev_hash") != prev:
                return False, n, f"line {line_no}: prev_hash does not match previous record (chain broken)"
            if rec.get("seq") != n + 1:
                return False, n, f"line {line_no}: sequence gap (expected {n + 1}, got {rec.get('seq')})"
            expected = hashlib.sha256((prev + _canonical(rec)).encode("utf-8")).hexdigest()
            if expected != claimed:
                return False, n, f"line {line_no}: hash mismatch (record was modified)"
            prev = claimed
            n += 1
    return True, n, f"audit chain intact ({n} records)"
