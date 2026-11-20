"""Telemetry store and tenant-scoped views."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from fleet_copilot.data.loader import LoadIssue, load_ndjson
from fleet_copilot.data.models import Snapshot, classify_device_type


class NotFoundInTenant(LookupError):
    """Raised for unknown IDs *and* IDs that belong to another tenant (indistinguishable to callers)."""

    def __init__(self, kind: str, identifier: str):
        self.kind = kind
        self.identifier = identifier
        super().__init__(f"{kind} '{identifier}' was not found in your organization's fleet.")


def company_display_name(company_id: str) -> str:
    """'acme-001' -> 'Acme' (derived from the ID; the dataset has no separate name field)."""
    return company_id.split("-")[0].capitalize()


@dataclass(frozen=True)
class DeviceProfile:
    device_id: str
    company_id: str
    employee_id: str | None
    device_type: str
    model_name: str | None
    hostname: str | None
    first_seen: str
    last_seen: str
    snapshot_count: int


class TelemetryStore:
    def __init__(self, snapshots: list[Snapshot], issues: list[LoadIssue] | None = None, source: str = ""):
        self.source = source
        self.load_issues = list(issues or [])
        self._by_company: dict[str, dict[str, list[Snapshot]]] = defaultdict(lambda: defaultdict(list))
        self._device_company: dict[str, str] = {}
        self._employee_company: dict[str, str] = {}
        self._hostname_company: dict[str, str] = {}
        for s in snapshots:
            self._by_company[s.company_id][s.device_id].append(s)
            self._device_company.setdefault(s.device_id, s.company_id)
            if s.employee_id:
                self._employee_company.setdefault(s.employee_id, s.company_id)
            if s.hostname:
                self._hostname_company.setdefault(s.hostname, s.company_id)
        for devices in self._by_company.values():
            for hist in devices.values():
                hist.sort(key=lambda x: x.collected_at)
        self.snapshot_count = len(snapshots)

    @classmethod
    def from_file(cls, path: str | Path) -> "TelemetryStore":
        res = load_ndjson(path)
        return cls(res.snapshots, res.issues, source=str(path))

    # ---- platform-level helpers (never exposed to the agent) ----
    def company_ids(self) -> list[str]:
        return sorted(self._by_company)

    def locate_device(self, device_id: str) -> str | None:
        return self._device_company.get(device_id)

    def locate_employee(self, employee_id: str) -> str | None:
        return self._employee_company.get(employee_id)

    def identifiers_by_company(self) -> dict[str, dict[str, set[str]]]:
        out: dict[str, dict[str, set[str]]] = {}
        for cid in self.company_ids():
            out[cid] = {
                "devices": {d for d, c in self._device_company.items() if c == cid},
                "employees": {e for e, c in self._employee_company.items() if c == cid},
                "hostnames": {h for h, c in self._hostname_company.items() if c == cid},
            }
        return out

    def tenant(self, company_id: str, on_cross_tenant: Callable[[str, str, str], None] | None = None) -> "TenantView":
        if company_id not in self._by_company:
            raise KeyError(f"Unknown tenant '{company_id}'")
        return TenantView(self, company_id, on_cross_tenant)

    def _tenant_devices(self, company_id: str) -> dict[str, list[Snapshot]]:
        return self._by_company[company_id]


class TenantView:
    """Read-only, tenant-scoped access to telemetry. The only data handle tools receive."""

    def __init__(self, store: TelemetryStore, company_id: str, on_cross_tenant: Callable[[str, str, str], None] | None):
        self._store = store
        self.tenant_id = company_id
        self.display_name = company_display_name(company_id)
        self._devices = store._tenant_devices(company_id)
        self._on_cross_tenant = on_cross_tenant
        all_ts = [s.collected_at for h in self._devices.values() for s in h]
        self.window_start: datetime = min(all_ts)
        self.window_end: datetime = max(all_ts)
        self._employees: dict[str, list[str]] = defaultdict(list)
        for did, hist in sorted(self._devices.items()):
            emp = hist[-1].employee_id
            if emp:
                self._employees[emp].append(did)

    # ---- identity / existence ----
    def _deny(self, kind: str, identifier: str) -> NotFoundInTenant:
        owner = self._store.locate_device(identifier) if kind == "device" else self._store.locate_employee(identifier)
        if owner and owner != self.tenant_id and self._on_cross_tenant:
            self._on_cross_tenant(kind, identifier, owner)
        return NotFoundInTenant(kind, identifier)

    def has_device(self, device_id: str) -> bool:
        return device_id in self._devices

    def has_employee(self, employee_id: str) -> bool:
        return employee_id in self._employees

    def require_device(self, device_id: str) -> None:
        if device_id not in self._devices:
            raise self._deny("device", device_id)

    def require_employee(self, employee_id: str) -> None:
        if employee_id not in self._employees:
            raise self._deny("employee", employee_id)

    # ---- data access ----
    def device_ids(self) -> list[str]:
        return sorted(self._devices)

    def history(self, device_id: str) -> list[Snapshot]:
        self.require_device(device_id)
        return list(self._devices[device_id])

    def latest(self, device_id: str) -> Snapshot:
        return self.history(device_id)[-1]

    def latest_all(self) -> list[Snapshot]:
        return [self._devices[d][-1] for d in self.device_ids()]

    def last_with_battery(self, device_id: str) -> Snapshot | None:
        for s in reversed(self.history(device_id)):
            if s.battery is not None:
                return s
        return None

    def employees(self) -> dict[str, list[str]]:
        return {e: list(ds) for e, ds in sorted(self._employees.items())}

    def employee_devices(self, employee_id: str) -> list[str]:
        self.require_employee(employee_id)
        return list(self._employees[employee_id])

    def profile(self, device_id: str) -> DeviceProfile:
        hist = self.history(device_id)
        last = hist[-1]
        return DeviceProfile(
            device_id=device_id,
            company_id=self.tenant_id,
            employee_id=last.employee_id,
            device_type=classify_device_type(last.model_name, any(s.battery for s in hist)),
            model_name=last.model_name,
            hostname=last.hostname,
            first_seen=hist[0].collected_at_iso,
            last_seen=last.collected_at_iso,
            snapshot_count=len(hist),
        )

    def device_type(self, device_id: str) -> str:
        return self.profile(device_id).device_type

    def check_catalog(self) -> dict[str, str]:
        """check_id -> severity, as observed in this tenant's telemetry."""
        cat: dict[str, str] = {}
        for hist in self._devices.values():
            for s in hist:
                for c in s.compliance:
                    cat.setdefault(c.check_id, c.severity)
        return dict(sorted(cat.items()))

    @property
    def as_of(self) -> str:
        return self.window_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def window_label(self) -> str:
        return f"{self.window_start.date().isoformat()} to {self.window_end.date().isoformat()}"
