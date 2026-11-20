"""Normalized telemetry snapshot models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fleet_copilot.config import GB

LAPTOP_MODEL_KEYWORDS = ("macbook", "thinkpad", "xps", "latitude", "elitebook", "surface laptop")
DESKTOP_MODEL_KEYWORDS = ("mac mini", "imac", "mac studio", "mac pro", "optiplex")


@dataclass(frozen=True)
class DiskVolume:
    volume_name: str
    mount_point: str
    file_system: str
    size_bytes: int
    available_bytes: int
    encrypted: bool | None

    @property
    def free_pct(self) -> float:
        return (self.available_bytes / self.size_bytes * 100.0) if self.size_bytes else 0.0

    @property
    def free_gb(self) -> float:
        return self.available_bytes / GB

    @property
    def size_gb(self) -> float:
        return self.size_bytes / GB


@dataclass(frozen=True)
class Battery:
    present: bool
    charging_status: str | None
    percentage: int | None
    condition: str | None
    cycle_count: int | None
    full_charge_capacity: int | None


@dataclass(frozen=True)
class ComplianceResult:
    check_id: str
    status: str
    severity: str


@dataclass(frozen=True)
class Software:
    name: str
    version: str
    publisher: str | None


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    source_line: int
    device_id: str
    company_id: str
    employee_id: str | None
    collected_at: datetime
    collected_at_iso: str
    agent_version: str | None
    os_platform: str | None
    os_name: str | None
    os_version: str | None
    os_build: str | None
    architecture: str | None
    hostname: str | None
    model_name: str | None
    model_identifier: str | None
    processor: str | None
    total_memory_label: str | None
    serial_number: str | None
    ram_bytes: int | None
    mem_total_bytes: int | None
    mem_used_bytes: int | None
    mem_free_bytes: int | None
    disks: tuple[DiskVolume, ...]
    battery: Battery | None
    network_reported: bool
    software: tuple[Software, ...]
    compliance: tuple[ComplianceResult, ...]

    @property
    def date(self) -> str:
        return self.collected_at_iso[:10]

    @property
    def mem_used_pct(self) -> float | None:
        if not self.mem_total_bytes or self.mem_used_bytes is None:
            return None
        return self.mem_used_bytes / self.mem_total_bytes * 100.0

    @property
    def ram_gb(self) -> float | None:
        total = self.ram_bytes or self.mem_total_bytes
        return total / GB if total else None

    @property
    def primary_disk(self) -> DiskVolume | None:
        """The system volume (`/` or `C:\\`), falling back to the largest volume."""
        if not self.disks:
            return None
        for d in self.disks:
            if d.mount_point in ("/", "C:\\", "C:/", "C:"):
                return d
        return max(self.disks, key=lambda d: d.size_bytes)

    @property
    def os_label(self) -> str:
        return f"{self.os_name or '?'} {self.os_version or ''}".strip()

    def compliance_status(self, check_id: str) -> ComplianceResult | None:
        for c in self.compliance:
            if c.check_id == check_id:
                return c
        return None

    def has_software(self, name_fragment: str) -> Software | None:
        frag = name_fragment.lower()
        for s in self.software:
            if frag in s.name.lower():
                return s
        return None


def classify_device_type(model_name: str | None, ever_had_battery: bool) -> str:
    """Laptop / desktop classification from the model name, falling back to battery presence."""
    name = (model_name or "").lower()
    if any(k in name for k in DESKTOP_MODEL_KEYWORDS):
        return "desktop"
    if any(k in name for k in LAPTOP_MODEL_KEYWORDS):
        return "laptop"
    return "laptop" if ever_had_battery else "unknown"
