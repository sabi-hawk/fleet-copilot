"""Load and validate the NDJSON telemetry export.

The loader is deliberately defensive: malformed lines are recorded as data
quality issues instead of crashing the copilot, and optional sections
(battery, network) are allowed to be missing because the dataset contains
such snapshots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fleet_copilot.data.models import Battery, ComplianceResult, DiskVolume, Snapshot, Software

REQUIRED_FIELDS = ("device_id", "company_id", "collected_at")


@dataclass
class LoadIssue:
    line: int
    problem: str


@dataclass
class LoadResult:
    snapshots: list[Snapshot]
    issues: list[LoadIssue] = field(default_factory=list)
    total_lines: int = 0


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_record(rec: dict[str, Any], line_no: int) -> Snapshot:
    for f in REQUIRED_FIELDS:
        if not rec.get(f):
            raise ValueError(f"missing required field '{f}'")
    os_ = rec.get("os") or {}
    ident = rec.get("device_identity") or {}
    mem = rec.get("memory") or {}
    disks = tuple(
        DiskVolume(
            volume_name=str(d.get("volume_name", "")),
            mount_point=str(d.get("mount_point", "")),
            file_system=str(d.get("file_system", "")),
            size_bytes=_int(d.get("size_bytes")) or 0,
            available_bytes=_int(d.get("available_bytes")) or 0,
            encrypted=d.get("encrypted"),
        )
        for d in (rec.get("disk_volumes") or [])
    )
    b = rec.get("battery")
    battery = None
    if isinstance(b, dict) and b.get("battery_present", True):
        battery = Battery(
            present=bool(b.get("battery_present", True)),
            charging_status=b.get("charging_status"),
            percentage=_int(b.get("percentage")),
            condition=b.get("condition"),
            cycle_count=_int(b.get("cycle_count")),
            full_charge_capacity=_int(b.get("full_charge_capacity")),
        )
    software = tuple(
        Software(name=str(s.get("name", "")), version=str(s.get("version", "")), publisher=s.get("publisher"))
        for s in (rec.get("installed_software") or [])
    )
    compliance = tuple(
        ComplianceResult(
            check_id=str(c.get("check_id")),
            status=str(c.get("status", "unknown")).lower(),
            severity=str(c.get("severity", "unknown")).lower(),
        )
        for c in (rec.get("compliance_results") or [])
        if c.get("check_id")
    )
    collected_at = rec["collected_at"]
    return Snapshot(
        snapshot_id=f"{rec['device_id']}@{collected_at}",
        source_line=line_no,
        device_id=rec["device_id"],
        company_id=rec["company_id"],
        employee_id=rec.get("employee_id"),
        collected_at=_parse_ts(collected_at),
        collected_at_iso=collected_at,
        agent_version=rec.get("agent_version"),
        os_platform=os_.get("platform"),
        os_name=os_.get("product_name"),
        os_version=os_.get("product_version"),
        os_build=os_.get("build_version"),
        architecture=os_.get("architecture"),
        hostname=os_.get("hostname"),
        model_name=ident.get("model_name"),
        model_identifier=ident.get("model_identifier"),
        processor=ident.get("processor"),
        total_memory_label=ident.get("total_memory"),
        serial_number=ident.get("serial_number"),
        ram_bytes=_int(mem.get("ram_bytes")),
        mem_total_bytes=_int(mem.get("total_memory_bytes")),
        mem_used_bytes=_int(mem.get("used_memory_bytes")),
        mem_free_bytes=_int(mem.get("free_memory_bytes")),
        disks=disks,
        battery=battery,
        network_reported=bool(rec.get("network")),
        software=software,
        compliance=compliance,
    )


def load_ndjson(path: str | Path) -> LoadResult:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Telemetry dataset not found: {path}")
    result = LoadResult(snapshots=[])
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            result.total_lines = line_no
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                snap = parse_record(rec, line_no)
            except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
                result.issues.append(LoadIssue(line=line_no, problem=f"unparseable record: {exc}"))
                continue
            if snap.snapshot_id in seen:
                result.issues.append(LoadIssue(line=line_no, problem=f"duplicate snapshot {snap.snapshot_id} (kept first)"))
                continue
            seen.add(snap.snapshot_id)
            result.snapshots.append(snap)
    return result
