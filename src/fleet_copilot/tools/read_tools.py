"""Read-only telemetry tools."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.analytics.trends import days_between, linear_slope, mean
from fleet_copilot.analytics.versions import compare_to_bound, normalize_family, parse_os
from fleet_copilot.data.models import Snapshot
from fleet_copilot.tools.base import ToolContext, ToolOutput, ToolSpec


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _r1(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def _row(ctx: ToolContext, s: Snapshot) -> dict:
    d = s.primary_disk
    bsnap = ctx.view.last_with_battery(s.device_id)
    battery = None
    if bsnap is not None:
        battery = {"condition": bsnap.battery.condition, "full_charge_capacity": bsnap.battery.full_charge_capacity,
                   "cycle_count": bsnap.battery.cycle_count, "as_of": bsnap.date}
    return {
        "device_id": s.device_id,
        "employee_id": s.employee_id,
        "device_type": ctx.view.device_type(s.device_id),
        "model": s.model_name,
        "hostname": s.hostname,
        "os": s.os_label,
        "ram_gb": _r1(s.ram_gb),
        "memory_used_pct": _r1(s.mem_used_pct),
        "disk_size_gb": round(d.size_gb) if d else None,
        "disk_free_gb": _r1(d.free_gb) if d else None,
        "disk_free_pct": _r1(d.free_pct) if d else None,
        "disk_encrypted": d.encrypted if d else None,
        "battery": battery,
        "failing_checks": [f"{c.check_id} ({c.severity})" for c in s.compliance if c.status == "fail"],
        "snapshot_date": s.date,
    }


def _device_claim(row: dict) -> str:
    parts = [f"{row['device_id']} latest snapshot {row['snapshot_date']}: {row['device_type']} {row['model']}, {row['os']}"]
    if row["disk_free_pct"] is not None:
        parts.append(f"free disk {row['disk_free_pct']}% ({row['disk_free_gb']} GB of {row['disk_size_gb']} GB)")
    if row["memory_used_pct"] is not None:
        parts.append(f"memory used {row['memory_used_pct']}% of {row['ram_gb']:.0f} GB")
    if row["battery"]:
        b = row["battery"]
        parts.append(f"battery '{b['condition']}' (as of {b['as_of']})")
    parts.append("failing checks: " + (", ".join(row["failing_checks"]) if row["failing_checks"] else "none"))
    parts.append(f"employee {row['employee_id']}")
    return "; ".join(parts)
class FleetOverviewArgs(_Args):
    pass


def fleet_overview(ctx: ToolContext, _: FleetOverviewArgs) -> ToolOutput:
    v = ctx.view
    latest = v.latest_all()
    types = Counter(v.device_type(s.device_id) for s in latest)
    oses = Counter(s.os_label for s in latest)
    models = Counter(s.model_name for s in latest)
    catalog = v.check_catalog()
    ev = [ctx.ledger.add(
        tool="get_fleet_overview",
        claim=(f"{v.display_name} fleet: {len(latest)} devices ({', '.join(f'{n} {t}s' for t, n in sorted(types.items()))}), "
               f"{len(v.employees())} employees; telemetry {v.window_label}; latest snapshot {v.as_of}"),
        snapshots=latest, fields={"devices": len(latest), "employees": len(v.employees())},
    )]
    checks = {}
    for chk, sev in catalog.items():
        failing = sorted(s.device_id for s in latest if (c := s.compliance_status(chk)) and c.status == "fail")
        checks[chk] = {"severity": sev, "failing_devices": len(failing), "total_devices": len(latest)}
        ev.append(ctx.ledger.add(
            tool="get_fleet_overview",
            claim=f"'{chk}' ({sev}) failing on {len(failing)} of {len(latest)} devices at their latest snapshots" + (f": {', '.join(failing)}" if failing else ""),
            snapshots=latest, fields={"check_id": chk, "failing": len(failing)},
        ))
    data = {
        "organization": v.display_name, "tenant_id": v.tenant_id, "devices": len(latest), "device_types": dict(types),
        "os_versions": dict(sorted(oses.items())), "models": dict(sorted(models.items())), "employees": len(v.employees()),
        "telemetry_window": v.window_label, "as_of": v.as_of, "snapshots_per_device": len(v.history(latest[0].device_id)),
        "compliance_checks": checks,
    }
    return ToolOutput(f"{len(latest)} devices, {len(v.employees())} employees, telemetry {v.window_label}.", data, ev)
class FindDevicesArgs(_Args):
    device_type: Literal["laptop", "desktop"] | None = Field(None, description="Restrict to laptops or desktops.")
    os_name: Literal["macOS", "Windows"] | None = Field(None, description="Restrict to an OS family.")
    os_version_below: str | None = Field(None, description="Only devices whose OS version is older than this, e.g. '15' or '14.5' (macOS) or '11' (Windows). Requires os_name.")
    os_version_at_least: str | None = Field(None, description="Only devices at or above this OS version. Requires os_name.")
    model_contains: str | None = Field(None, description="Case-insensitive substring of the hardware model, e.g. 'MacBook'.")
    low_disk: bool | None = Field(None, description="True = only devices whose latest free disk is below the configured low-disk threshold (15%).")
    max_disk_free_pct: float | None = Field(None, ge=0, le=100, description="Only devices with latest free disk percentage at or below this value.")
    min_memory_used_pct: float | None = Field(None, ge=0, le=100, description="Only devices whose latest memory utilisation is at or above this value.")
    failing_check_id: str | None = Field(None, description="Only devices currently failing this compliance check (e.g. 'screen_lock').")
    failing_severity: Literal["high", "medium", "low"] | None = Field(None, description="Only devices currently failing at least one check of this severity.")
    battery_not_normal: bool | None = Field(None, description="True = only devices whose last reported battery condition is not 'Normal'.")
    employee_id: str | None = Field(None, description="Only devices assigned to this employee.")
    software_contains: str | None = Field(None, description="Only devices whose latest snapshot lists software whose name contains this text.")


def find_devices(ctx: ToolContext, a: FindDevicesArgs) -> ToolOutput:
    v, th = ctx.view, ctx.settings.thresholds
    if (a.os_version_below or a.os_version_at_least) and not a.os_name:
        raise ValueError("os_version_below/os_version_at_least require os_name ('macOS' or 'Windows')")
    if a.employee_id:
        v.require_employee(a.employee_id)
    latest = v.latest_all()
    criteria = {k: val for k, val in a.model_dump().items() if val is not None}
    matched, not_comparable = [], []
    for s in latest:
        d = s.primary_disk
        if a.device_type and v.device_type(s.device_id) != a.device_type:
            continue
        if a.os_name and normalize_family(s.os_name) != a.os_name:
            if a.os_version_below or a.os_version_at_least:
                not_comparable.append(s.device_id)
            continue
        osv = parse_os(s.os_name, s.os_version)
        if a.os_version_below and compare_to_bound(osv, a.os_name, a.os_version_below) != -1:
            continue
        if a.os_version_at_least and compare_to_bound(osv, a.os_name, a.os_version_at_least) not in (0, 1):
            continue
        if a.model_contains and a.model_contains.lower() not in (s.model_name or "").lower():
            continue
        if a.low_disk and not (d and d.free_pct < th.low_disk_free_pct):
            continue
        if a.max_disk_free_pct is not None and not (d and d.free_pct <= a.max_disk_free_pct):
            continue
        if a.min_memory_used_pct is not None and not ((s.mem_used_pct or 0) >= a.min_memory_used_pct):
            continue
        if a.failing_check_id and not ((c := s.compliance_status(a.failing_check_id)) and c.status == "fail"):
            continue
        if a.failing_severity and not any(c.status == "fail" and c.severity == a.failing_severity for c in s.compliance):
            continue
        if a.battery_not_normal:
            b = v.last_with_battery(s.device_id)
            if not (b and (b.battery.condition or "").lower() != "normal"):
                continue
        if a.employee_id and s.employee_id != a.employee_id:
            continue
        if a.software_contains and not s.has_software(a.software_contains):
            continue
        matched.append(s)
    rows, ev = [], []
    for s in matched:
        row = _row(ctx, s)
        snaps = [s]
        if row["battery"] and row["battery"]["as_of"] != s.date:
            snaps.append(v.last_with_battery(s.device_id))
        row["evidence_id"] = ctx.ledger.add(tool="find_devices", claim=_device_claim(row), device_id=s.device_id, snapshots=snaps,
                                            fields={k: row[k] for k in ("os", "disk_free_pct", "memory_used_pct", "failing_checks")})
        rows.append(row)
        ev.append(row["evidence_id"])
    crit_txt = ", ".join(f"{k}={val}" for k, val in criteria.items()) or "no filters"
    if a.low_disk:
        crit_txt += f" (low disk = free < {th.low_disk_free_pct:.0f}%)"
    scope_claim = (f"Evaluated the latest snapshots of all {len(latest)} {v.display_name} devices (as of {v.as_of}) against [{crit_txt}]: "
                   f"{len(rows)} matched" + (f" ({', '.join(r['device_id'] for r in rows)})" if rows else ""))
    if not_comparable:
        scope_claim += f"; {len(not_comparable)} devices run a different OS family and were not compared"
    scope_id = ctx.ledger.add(tool="find_devices", claim=scope_claim, snapshots=latest, fields={"criteria": criteria, "matched": len(rows)})
    data = {"criteria": criteria, "matched_count": len(rows), "evaluated_devices": len(latest), "scope_evidence_id": scope_id,
            "not_comparable_other_os_family": sorted(not_comparable), "devices": rows, "as_of": v.as_of}
    return ToolOutput(scope_claim, data, [scope_id] + ev)
class DeviceDetailsArgs(_Args):
    device_id: str = Field(..., description="Device ID (serial), e.g. '1LYSSFD074BB'.")


def device_details(ctx: ToolContext, a: DeviceDetailsArgs) -> ToolOutput:
    v = ctx.view
    s = v.latest(a.device_id)
    prof = v.profile(a.device_id)
    row = _row(ctx, s)
    led = ctx.ledger
    ev = [led.add(tool="get_device_details", claim=_device_claim(row), device_id=s.device_id, snapshots=[s],
                  fields={"os": row["os"], "model": row["model"], "employee_id": row["employee_id"]})]
    comp = [{"check_id": c.check_id, "status": c.status, "severity": c.severity} for c in s.compliance]
    ev.append(led.add(tool="get_device_details",
                      claim=f"{s.device_id} compliance on {s.date}: " + ", ".join(f"{c['check_id']}={c['status']} ({c['severity']})" for c in comp),
                      device_id=s.device_id, snapshots=[s], fields={"compliance": comp}))
    software = [f"{sw.name} {sw.version}" for sw in s.software]
    ev.append(led.add(tool="get_device_details", claim=f"{s.device_id} installed software on {s.date}: {', '.join(software)}",
                      device_id=s.device_id, snapshots=[s], fields={"installed_software": software}))
    b = v.last_with_battery(s.device_id)
    if b is not None:
        ev.append(led.add(tool="get_device_details",
                          claim=(f"{s.device_id} battery on {b.date}: condition '{b.battery.condition}', full_charge_capacity "
                                 f"{b.battery.full_charge_capacity}, cycle_count {b.battery.cycle_count}"),
                          device_id=s.device_id, snapshots=[b],
                          fields={"battery.condition": b.battery.condition, "battery.full_charge_capacity": b.battery.full_charge_capacity}))
    data = {**row, "processor": s.processor, "architecture": s.architecture, "os_build": s.os_build, "agent_version": s.agent_version,
            "compliance": comp, "installed_software": software, "first_seen": prof.first_seen, "last_seen": prof.last_seen,
            "snapshot_count": prof.snapshot_count,
            "battery_note": None if b is None or b is s else f"latest snapshot has no battery section; battery data is from {b.date}"}
    return ToolOutput(f"Details for {s.device_id} as of {s.date}.", data, ev)
METRICS = Literal["disk_free_pct", "disk_free_gb", "memory_used_pct", "battery_full_charge_capacity",
                  "battery_cycle_count", "battery_condition", "compliance_status"]


class MetricHistoryArgs(_Args):
    device_id: str = Field(..., description="Device ID.")
    metric: METRICS = Field(..., description="Metric to retrieve over time.")
    check_id: str | None = Field(None, description="Required when metric='compliance_status'.")
    last_n_days: int | None = Field(None, ge=1, le=365, description="Limit to the most recent N days of the window.")


def _metric_value(s: Snapshot, metric: str, check_id: str | None):
    d = s.primary_disk
    if metric == "disk_free_pct":
        return round(d.free_pct, 1) if d else None
    if metric == "disk_free_gb":
        return round(d.free_gb, 1) if d else None
    if metric == "memory_used_pct":
        return round(s.mem_used_pct, 1) if s.mem_used_pct is not None else None
    if metric.startswith("battery_"):
        if s.battery is None:
            return None
        return {"battery_full_charge_capacity": s.battery.full_charge_capacity, "battery_cycle_count": s.battery.cycle_count,
                "battery_condition": s.battery.condition}[metric]
    if metric == "compliance_status":
        c = s.compliance_status(check_id or "")
        return c.status if c else None
    raise ValueError(f"unknown metric {metric}")


def metric_history(ctx: ToolContext, a: MetricHistoryArgs) -> ToolOutput:
    if a.metric == "compliance_status":
        if not a.check_id:
            raise ValueError("check_id is required for metric 'compliance_status'")
        if a.check_id not in ctx.view.check_catalog():
            raise ValueError(f"unknown check_id '{a.check_id}'; known: {', '.join(ctx.view.check_catalog())}")
    hist = ctx.view.history(a.device_id)
    if a.last_n_days:
        cutoff = hist[-1].collected_at.timestamp() - a.last_n_days * 86400
        hist = [s for s in hist if s.collected_at.timestamp() > cutoff]
    pts = [(s, _metric_value(s, a.metric, a.check_id)) for s in hist]
    present = [(s, val) for s, val in pts if val is not None]
    missing = [s.date for s, val in pts if val is None]
    led, did, label = ctx.ledger, a.device_id, a.metric + (f"[{a.check_id}]" if a.check_id else "")
    if not present:
        eid = led.add(tool="get_metric_history", claim=f"{did} has no '{label}' readings in {len(pts)} snapshots", device_id=did,
                      snapshots=[s for s, _ in pts], fields={"metric": label, "readings": 0})
        return ToolOutput(f"No '{label}' readings for {did}.", {"device_id": did, "metric": label, "series": [], "missing_dates": missing}, [eid])
    series = [{"date": s.date, "value": val} for s, val in present]
    ev, summary = [], {}
    numeric = isinstance(present[0][1], (int, float))
    if numeric:
        vals = [float(val) for _, val in present]
        xs = [days_between(present[0][0].collected_at, s.collected_at) for s, _ in present]
        slope = linear_slope(xs, vals)
        mn = min(present, key=lambda p: p[1])
        mx = max(present, key=lambda p: p[1])
        summary = {"first": present[0][1], "last": present[-1][1], "min": mn[1], "max": mx[1], "mean": round(mean(vals), 1),
                   "trend_per_day": round(slope, 2), "readings": len(present)}
        points = {"first": present[0], "last": present[-1], "min": mn, "max": mx}
        for kind, (s, val) in points.items():
            ev.append(led.add(tool="get_metric_history", claim=f"{did} {label} = {val} on {s.date} ({kind} of window)", device_id=did,
                              snapshots=[s], fields={label: val}))
        ev.append(led.add(tool="get_metric_history",
                          claim=(f"{did} {label} from {present[0][0].date} to {present[-1][0].date}: {len(present)} readings, "
                                 f"mean {summary['mean']}, trend {slope:+.2f}/day"),
                          device_id=did, snapshots=[s for s, _ in present], fields=summary,
                          period=[present[0][0].collected_at_iso, present[-1][0].collected_at_iso]))
    else:
        changes = [present[0]] + [present[i] for i in range(1, len(present)) if present[i][1] != present[i - 1][1]]
        summary = {"first": present[0][1], "last": present[-1][1], "readings": len(present), "changes": len(changes) - 1,
                   "counts": dict(Counter(val for _, val in present))}
        for s, val in changes + ([present[-1]] if present[-1] not in changes else []):
            ev.append(led.add(tool="get_metric_history", claim=f"{did} {label} = '{val}' on {s.date}", device_id=did, snapshots=[s],
                              fields={label: val}))
        summary["transitions"] = [{"date": s.date, "value": val} for s, val in changes]
    data = {"device_id": did, "metric": label, "summary": summary, "series": series, "missing_dates": missing}
    return ToolOutput(f"{len(present)} '{label}' readings for {did} ({present[0][0].date} to {present[-1][0].date}).", data, ev)
class ComplianceStatusArgs(_Args):
    check_id: str | None = Field(None, description="Restrict to one check (e.g. 'disk_encryption'). Omit for all checks.")
    severity: Literal["high", "medium", "low"] | None = Field(None, description="Restrict to checks of this severity.")
    status: Literal["fail", "pass"] = Field("fail", description="Which devices to list (default: failing).")
    device_type: Literal["laptop", "desktop"] | None = Field(None, description="Restrict to laptops or desktops.")


def compliance_status(ctx: ToolContext, a: ComplianceStatusArgs) -> ToolOutput:
    v = ctx.view
    catalog = v.check_catalog()
    if a.check_id and a.check_id not in catalog:
        raise ValueError(f"unknown check_id '{a.check_id}'; known checks: {', '.join(f'{k} ({s})' for k, s in catalog.items())}")
    checks = [c for c, sev in catalog.items() if (not a.check_id or c == a.check_id) and (not a.severity or sev == a.severity)]
    latest = [s for s in v.latest_all() if not a.device_type or v.device_type(s.device_id) == a.device_type]
    scope = f"{len(latest)} {a.device_type + 's' if a.device_type else 'devices'}"
    per_check, rows, ev = {}, [], []
    if not checks:
        eid = ctx.ledger.add(tool="get_compliance_status",
                             claim=f"No {a.severity or ''} compliance checks exist in {v.display_name} telemetry (catalog: {', '.join(f'{k} ({s})' for k, s in catalog.items())})",
                             snapshots=latest, fields={"catalog": catalog})
        return ToolOutput("No matching checks.", {"catalog": catalog, "checks": {}, "devices": []}, [eid])
    for chk in checks:
        listed = sorted((s for s in latest if (c := s.compliance_status(chk)) and c.status == a.status), key=lambda s: s.device_id)
        per_check[chk] = {"severity": catalog[chk], a.status: len(listed), "of": len(latest)}
        ev.append(ctx.ledger.add(
            tool="get_compliance_status",
            claim=(f"'{chk}' ({catalog[chk]} severity) has status '{a.status}' on {len(listed)} of {scope} at their latest snapshots"
                   f" (as of {v.as_of})" + (f": {', '.join(s.device_id for s in listed)}" if listed else "")),
            snapshots=latest, fields={"check_id": chk, a.status: len(listed), "of": len(latest)}))
        for s in listed:
            eid = ctx.ledger.add(tool="get_compliance_status",
                                 claim=f"{s.device_id} '{chk}' status '{a.status}' on {s.date} (latest snapshot)",
                                 device_id=s.device_id, snapshots=[s],
                                 fields={f"compliance.{chk}.status": a.status, "employee_id": s.employee_id})
            rows.append({"device_id": s.device_id, "employee_id": s.employee_id, "device_type": v.device_type(s.device_id),
                         "check_id": chk, "severity": catalog[chk], "status": a.status, "snapshot_date": s.date, "evidence_id": eid})
            ev.append(eid)
    data = {"catalog": catalog, "checks": per_check, "devices": rows, "as_of": v.as_of, "scope": scope}
    total = len(rows)
    return ToolOutput(f"{total} device/check pairs with status '{a.status}' across {len(checks)} check(s) and {scope}.", data, ev)
class SoftwareSearchArgs(_Args):
    name_contains: str = Field(..., min_length=2, description="Case-insensitive substring of the application name, e.g. 'uTorrent'.")


def software_search(ctx: ToolContext, a: SoftwareSearchArgs) -> ToolOutput:
    v = ctx.view
    rows, ev = [], []
    latest = v.latest_all()
    for s in latest:
        sw = s.has_software(a.name_contains)
        if not sw:
            continue
        hist = v.history(s.device_id)
        first = next(h for h in hist if h.has_software(a.name_contains))
        eid = ctx.ledger.add(tool="search_installed_software",
                             claim=f"{s.device_id} has '{sw.name} {sw.version}' installed on {s.date} (first seen {first.date})",
                             device_id=s.device_id, snapshots=[first, s],
                             fields={"software": sw.name, "version": sw.version, "employee_id": s.employee_id})
        rows.append({"device_id": s.device_id, "employee_id": s.employee_id, "software": sw.name, "version": sw.version,
                     "publisher": sw.publisher, "first_seen": first.date, "present_since_start": first is hist[0], "evidence_id": eid})
        ev.append(eid)
    scope = ctx.ledger.add(tool="search_installed_software",
                           claim=f"'{a.name_contains}' found on {len(rows)} of {len(latest)} devices at their latest snapshots (as of {v.as_of})",
                           snapshots=latest, fields={"query": a.name_contains, "matches": len(rows)})
    return ToolOutput(f"'{a.name_contains}' installed on {len(rows)} of {len(latest)} devices.", {"query": a.name_contains, "devices": rows}, [scope] + ev)
class EmployeeDevicesArgs(_Args):
    employee_id: str = Field(..., description="Employee ID, e.g. 'emp-acme-1002'.")


def employee_devices(ctx: ToolContext, a: EmployeeDevicesArgs) -> ToolOutput:
    v = ctx.view
    rows, ev = [], []
    for did in v.employee_devices(a.employee_id):
        row = _row(ctx, v.latest(did))
        row["evidence_id"] = ctx.ledger.add(tool="get_employee_devices", claim=_device_claim(row), device_id=did, snapshots=[v.latest(did)],
                                            fields={"employee_id": a.employee_id})
        rows.append(row)
        ev.append(row["evidence_id"])
    return ToolOutput(f"{a.employee_id} is assigned {len(rows)} device(s).", {"employee_id": a.employee_id, "devices": rows}, ev)


READ_TOOLS = [
    ToolSpec("get_fleet_overview",
             "Summarise the organisation's fleet: device counts by type/OS/model, employees, telemetry window, and per-check "
             "compliance failure counts at each device's latest snapshot. Use for 'how many', 'overview' or context questions.",
             FleetOverviewArgs, "read", fleet_overview),
    ToolSpec("find_devices",
             "Filter devices on their LATEST snapshot by type, OS family/version (platform-aware comparison), model, disk, memory, "
             "failing checks, battery condition, employee or installed software. Always returns a scope evidence item describing how "
             "many devices were evaluated, so empty results can be cited as 'none'.",
             FindDevicesArgs, "read", find_devices),
    ToolSpec("get_device_details",
             "Full latest-state record for one device: hardware, OS, memory, disk, battery (last reading), compliance results and "
             "installed software.", DeviceDetailsArgs, "read", device_details),
    ToolSpec("get_metric_history",
             "Time series of one metric for one device across the 30-day window, with first/last/min/max/mean and linear trend "
             "(numeric metrics) or status transitions (battery_condition, compliance_status).", MetricHistoryArgs, "read", metric_history),
    ToolSpec("get_compliance_status",
             "Compliance check catalog (check_id -> severity) and which devices pass/fail each check at their latest snapshot. "
             "Filter by check_id, severity, status and device type.", ComplianceStatusArgs, "read", compliance_status),
    ToolSpec("search_installed_software",
             "Find devices whose latest snapshot lists an application (substring match), with the date it first appeared.",
             SoftwareSearchArgs, "read", software_search),
    ToolSpec("get_employee_devices",
             "List the devices assigned to an employee with their latest status (use before notifying an employee).",
             EmployeeDevicesArgs, "read", employee_devices),
]
