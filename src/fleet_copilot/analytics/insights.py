"""Deterministic insight & trend detectors over a tenant's telemetry time series.

Each detector returns `Insight` objects carrying (1) the finding, (2) the
supporting evidence as concrete snapshots, and (3) a brief explanation, which
is the structure the assessment asks for. Detectors are pure functions of the
`TenantView` and `Thresholds`, so their output is fully reproducible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fleet_copilot.analytics.trends import days_between, linear_slope, mean, round1
from fleet_copilot.config import Thresholds
from fleet_copilot.data.models import Snapshot
from fleet_copilot.data.store import TenantView
from fleet_copilot.evidence import EvidenceSpec

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
CATEGORIES = (
    "battery_health",
    "memory_pressure",
    "storage_pressure",
    "compliance_drift",
    "software_change",
    "telemetry_gap",
)


@dataclass
class Insight:
    category: str
    severity: str
    title: str
    finding: str
    explanation: str
    device_id: str | None = None
    employee_id: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceSpec] = field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)  # for fleet-level insights

    @property
    def insight_id(self) -> str:
        suffix = self.device_id or "fleet"
        extra = self.metrics.get("check_id") or self.metrics.get("software") or ""
        return f"{self.category}:{suffix}{':' + str(extra) if extra else ''}"


def _sort(insights: list[Insight]) -> list[Insight]:
    return sorted(insights, key=lambda i: (SEVERITY_RANK.get(i.severity, 9), i.category, i.device_id or "", i.insight_id))


def _next_tier(gb: float, tiers: tuple[int, ...]) -> str | None:
    for t in tiers:
        if t > gb + 0.5:
            return f"{t} GB" if t < 1024 else f"{t // 1024} TB"
    return None


# --------------------------------------------------------------------------- battery
def detect_battery_health(view: TenantView, th: Thresholds) -> list[Insight]:
    out: list[Insight] = []
    for did in view.device_ids():
        hist = view.history(did)
        bs = [s for s in hist if s.battery and s.battery.full_charge_capacity]
        if len(bs) < 2:
            continue
        first, last = bs[0], bs[-1]
        fcc0, fcc1 = first.battery.full_charge_capacity, last.battery.full_charge_capacity
        fade = (fcc0 - fcc1) / fcc0 * 100.0 if fcc0 else 0.0
        slope = linear_slope([days_between(first.collected_at, s.collected_at) for s in bs],
                             [s.battery.full_charge_capacity for s in bs])
        cond_latest = last.battery.condition or "unknown"
        latest_degraded = cond_latest.lower() != "normal"
        degraded = [s for s in bs if (s.battery.condition or "").lower() not in ("", "normal")]
        first_degraded = degraded[0] if degraded else None
        cycles = [s.battery.cycle_count for s in bs if s.battery.cycle_count is not None]
        max_cycles = max(cycles) if cycles else None
        fading = fade >= th.battery_fade_alert_pct
        if not (latest_degraded or fading):
            continue
        severity = "high" if (latest_degraded and fading) else "medium"
        parts = []
        if latest_degraded:
            since = f" since {first_degraded.date}" if first_degraded else ""
            parts.append(f"the OS reports battery condition '{cond_latest}'{since}")
        parts.append(f"full_charge_capacity fell {fade:.1f}% ({fcc0} -> {fcc1}) between {first.date} and {last.date}")
        finding = f"{did}: " + " and ".join(parts) + "."
        explanation = (
            "Two independent signals agree: the battery-health flag left 'Normal' during the window and the measured "
            f"full-charge capacity declined steadily (about {abs(slope):.1f} units/day), which is consistent with cell wear."
            if latest_degraded and fading
            else "Only one battery-wear signal is present, so this is a watch item rather than a confirmed end-of-life case."
        )
        if max_cycles is not None:
            explanation += f" Highest reported cycle_count: {max_cycles} (cycle counts in this export are noisy, so they are context only)."
        if hist[-1].battery is None:
            explanation += f" Note: the latest snapshot ({hist[-1].date}) has no battery section; last battery reading is from {last.date}."
        ev = [
            EvidenceSpec(
                claim=f"{did} battery on {first.date}: condition '{first.battery.condition}', full_charge_capacity {fcc0}, cycle_count {first.battery.cycle_count}",
                device_id=did, snapshots=[first],
                fields={"battery.condition": first.battery.condition, "battery.full_charge_capacity": fcc0, "battery.cycle_count": first.battery.cycle_count},
            )
        ]
        if first_degraded and first_degraded is not last:
            ev.append(EvidenceSpec(
                claim=f"{did} battery condition first reported as '{first_degraded.battery.condition}' on {first_degraded.date}",
                device_id=did, snapshots=[first_degraded],
                fields={"battery.condition": first_degraded.battery.condition},
            ))
        ev.append(EvidenceSpec(
            claim=f"{did} battery on {last.date}: condition '{cond_latest}', full_charge_capacity {fcc1}, cycle_count {last.battery.cycle_count}",
            device_id=did, snapshots=[last],
            fields={"battery.condition": cond_latest, "battery.full_charge_capacity": fcc1, "battery.cycle_count": last.battery.cycle_count},
        ))
        actions = [
            {"tool": "create_upgrade_order", "args": {"device_id": did, "component": "battery", "spec": "replacement battery (OEM)"}},
            {"tool": "flag_device_for_replacement", "args": {"device_id": did, "reason": f"Battery end-of-life: condition '{cond_latest}', capacity down {fade:.1f}% in {len(bs)} readings"}},
        ]
        out.append(Insight(
            category="battery_health", severity=severity, title="Battery approaching end-of-life",
            finding=finding, explanation=explanation, device_id=did, employee_id=hist[-1].employee_id,
            metrics={"condition_latest": cond_latest, "condition_degraded_since": first_degraded.date if first_degraded else None,
                     "fcc_first": fcc0, "fcc_last": fcc1, "fade_pct": round1(fade), "fcc_slope_per_day": round(slope, 2),
                     "max_cycle_count": max_cycles, "readings": len(bs)},
            evidence=ev, recommended_actions=actions,
        ))
    return _sort(out)


# --------------------------------------------------------------------------- memory
def memory_profile(hist: list[Snapshot], th: Thresholds) -> dict[str, Any]:
    pts = [(s, s.mem_used_pct) for s in hist if s.mem_used_pct is not None]
    vals = [v for _, v in pts]
    high = [(s, v) for s, v in pts if v >= th.high_memory_used_pct]
    peak_s, peak_v = max(pts, key=lambda p: p[1]) if pts else (None, 0.0)
    share = len(high) / len(pts) if pts else 0.0
    return {
        "snapshots": len(pts), "at_or_above": len(high), "share": share, "mean_pct": mean(vals),
        "peak_pct": peak_v, "peak_snapshot": peak_s, "latest_pct": vals[-1] if vals else None,
        "sustained": share >= th.memory_sustained_share, "watch": th.memory_watch_share <= share < th.memory_sustained_share,
    }


def detect_memory_pressure(view: TenantView, th: Thresholds) -> list[Insight]:
    out: list[Insight] = []
    tiers = (8, 16, 32, 64, 128)
    for did in view.device_ids():
        hist = view.history(did)
        p = memory_profile(hist, th)
        if not (p["sustained"] or p["watch"]):
            continue
        last = hist[-1]
        ram = last.ram_gb or 0
        kind = "consistently" if p["sustained"] else "intermittently"
        finding = (
            f"{did}: at or above {th.high_memory_used_pct:.0f}% RAM used in {p['at_or_above']}/{p['snapshots']} "
            f"snapshots (mean {p['mean_pct']:.1f}%, peak {p['peak_pct']:.1f}% on {p['peak_snapshot'].date}) with {ram:.0f} GB installed."
        )
        explanation = (
            "Memory utilisation exceeded the threshold in most daily snapshots across the window, so this is a persistent "
            "pattern rather than a one-off spike; the device is regularly operating near its memory capacity."
            if p["sustained"] else
            "Utilisation crosses the threshold on a minority of days; worth watching, but not yet a sustained constraint."
        )
        ev = [
            EvidenceSpec(
                claim=(f"{did} memory used >= {th.high_memory_used_pct:.0f}% in {p['at_or_above']} of {p['snapshots']} snapshots "
                       f"from {hist[0].date} to {last.date} (mean {p['mean_pct']:.1f}%)"),
                device_id=did, snapshots=hist,
                fields={"snapshots_constrained": p["at_or_above"], "snapshots_total": p["snapshots"],
                        "mean_used_pct": round1(p["mean_pct"]), "threshold_pct": th.high_memory_used_pct},
                period=[hist[0].collected_at_iso, last.collected_at_iso],
            ),
            EvidenceSpec(
                claim=f"{did} peak memory use {p['peak_pct']:.1f}% of {ram:.0f} GB on {p['peak_snapshot'].date}",
                device_id=did, snapshots=[p["peak_snapshot"]],
                fields={"memory.used_pct": round1(p["peak_pct"]), "memory.ram_gb": ram},
            ),
        ]
        actions = []
        spec = _next_tier(ram, tiers)
        if p["sustained"] and spec:
            actions.append({"tool": "create_upgrade_order", "args": {"device_id": did, "component": "memory", "spec": spec}})
        out.append(Insight(
            category="memory_pressure", severity="high" if p["sustained"] else "medium",
            title=f"RAM {kind} constrained", finding=finding, explanation=explanation,
            device_id=did, employee_id=last.employee_id,
            metrics={"ram_gb": ram, "constrained_snapshots": p["at_or_above"], "total_snapshots": p["snapshots"],
                     "share_constrained": round(p["share"], 3), "mean_used_pct": round1(p["mean_pct"]),
                     "peak_used_pct": round1(p["peak_pct"]), "pattern": "sustained" if p["sustained"] else "intermittent"},
            evidence=ev, recommended_actions=actions,
        ))
    return _sort(out)


# --------------------------------------------------------------------------- storage
def storage_profile(hist: list[Snapshot], th: Thresholds) -> dict[str, Any] | None:
    pts = [(s, s.primary_disk) for s in hist if s.primary_disk is not None]
    if not pts:
        return None
    first_s, first_d = pts[0]
    last_s, last_d = pts[-1]
    below = [(s, d) for s, d in pts if d.free_pct < th.low_disk_free_pct]
    slope = linear_slope([days_between(first_s.collected_at, s.collected_at) for s, _ in pts], [d.free_gb for _, d in pts])
    critical_gb = last_d.size_gb * th.critical_disk_free_pct / 100.0
    if last_d.free_pct < th.critical_disk_free_pct:
        days_to_critical = 0.0
    elif slope < 0:
        days_to_critical = (last_d.free_gb - critical_gb) / -slope
    else:
        days_to_critical = None
    days_to_full = (last_d.free_gb / -slope) if slope < 0 else None
    share = len(below) / len(pts)
    return {
        "first": first_s, "first_disk": first_d, "last": last_s, "last_disk": last_d, "below": below,
        "share_below": share, "slope_gb_per_day": slope, "days_to_critical": days_to_critical, "days_to_full": days_to_full,
        "consistent": share >= th.storage_consistency_share, "n": len(pts),
    }


def detect_storage_pressure(view: TenantView, th: Thresholds) -> list[Insight]:
    out: list[Insight] = []
    tiers = (128, 256, 512, 1024, 2048, 4096)
    for did in view.device_ids():
        hist = view.history(did)
        p = storage_profile(hist, th)
        if p is None:
            continue
        ld, fd = p["last_disk"], p["first_disk"]
        projected = p["days_to_critical"] is not None and p["days_to_critical"] <= th.storage_horizon_days and p["slope_gb_per_day"] < 0
        low_now = ld.free_pct < th.low_disk_free_pct
        if not (low_now or p["consistent"] or projected):
            continue
        severity = "critical" if ld.free_pct < th.critical_disk_free_pct else ("high" if low_now else "medium")
        title = ("Storage consistently low" if p["consistent"] else
                 "Storage low" if low_now else "Storage trending toward exhaustion")
        finding = (
            f"{did}: {ld.free_pct:.1f}% free ({ld.free_gb:.1f} GB of {ld.size_gb:.0f} GB) on {p['last'].date}; "
            f"below {th.low_disk_free_pct:.0f}% free in {len(p['below'])}/{p['n']} snapshots; free space trend "
            f"{p['slope_gb_per_day']:+.1f} GB/day (was {fd.free_gb:.1f} GB, {fd.free_pct:.1f}% on {p['first'].date})."
        )
        if p["slope_gb_per_day"] < -0.5 and p["days_to_full"] is not None:
            explanation = (f"Free space has fallen steadily across the window; at the fitted rate the volume would be full in "
                           f"about {p['days_to_full']:.0f} days. Sustained low free space blocks OS updates and risks data loss.")
        else:
            explanation = "Free space is persistently below the threshold without a clear recovery."
        ev = [
            EvidenceSpec(claim=f"{did} free disk {fd.free_pct:.1f}% ({fd.free_gb:.1f} GB of {fd.size_gb:.0f} GB) on {p['first'].date}",
                         device_id=did, snapshots=[p["first"]],
                         fields={"disk.free_pct": round1(fd.free_pct), "disk.available_bytes": fd.available_bytes, "disk.size_bytes": fd.size_bytes}),
            EvidenceSpec(claim=f"{did} free disk {ld.free_pct:.1f}% ({ld.free_gb:.1f} GB of {ld.size_gb:.0f} GB) on {p['last'].date}",
                         device_id=did, snapshots=[p["last"]],
                         fields={"disk.free_pct": round1(ld.free_pct), "disk.available_bytes": ld.available_bytes, "disk.size_bytes": ld.size_bytes}),
            EvidenceSpec(claim=(f"{did} free disk below {th.low_disk_free_pct:.0f}% in {len(p['below'])} of {p['n']} snapshots "
                                f"({hist[0].date} to {hist[-1].date}); fitted trend {p['slope_gb_per_day']:+.1f} GB/day"),
                         device_id=did, snapshots=hist,
                         fields={"snapshots_below": len(p["below"]), "snapshots_total": p["n"], "slope_gb_per_day": round(p["slope_gb_per_day"], 2)},
                         period=[hist[0].collected_at_iso, hist[-1].collected_at_iso]),
        ]
        actions = []
        emp = hist[-1].employee_id
        if emp:
            actions.append({"tool": "notify_employee", "args": {"employee_id": emp, "message": (
                f"Your device {did} has only {ld.free_pct:.1f}% free disk space ({ld.free_gb:.1f} GB). "
                "Please remove unneeded files or contact IT; low disk space can block updates.")}})
        spec = _next_tier(ld.size_gb, tiers)
        if spec:
            actions.append({"tool": "create_upgrade_order", "args": {"device_id": did, "component": "storage", "spec": spec}})
        out.append(Insight(
            category="storage_pressure", severity=severity, title=title, finding=finding, explanation=explanation,
            device_id=did, employee_id=emp,
            metrics={"free_pct_latest": round1(ld.free_pct), "free_gb_latest": round1(ld.free_gb), "size_gb": round(ld.size_gb),
                     "free_pct_first": round1(fd.free_pct), "snapshots_below_threshold": len(p["below"]), "total_snapshots": p["n"],
                     "slope_gb_per_day": round(p["slope_gb_per_day"], 2),
                     "days_to_full": round(p["days_to_full"]) if p["days_to_full"] is not None else None},
            evidence=ev, recommended_actions=actions,
        ))
    return _sort(out)


# --------------------------------------------------------------------------- compliance
def _status_runs(hist: list[Snapshot], check_id: str) -> list[tuple[Snapshot, str]]:
    return [(s, c.status) for s in hist if (c := s.compliance_status(check_id)) is not None]


def detect_compliance_drift(view: TenantView, th: Thresholds, check_id: str | None = None) -> list[Insight]:
    catalog = view.check_catalog()
    checks = [check_id] if check_id else list(catalog)
    out: list[Insight] = []
    for chk in checks:
        if chk not in catalog:
            continue
        sev = catalog[chk]
        chronic: list[tuple[str, Snapshot, Snapshot]] = []
        by_date_fail: dict[str, set[str]] = defaultdict(set)
        by_date_all: dict[str, set[str]] = defaultdict(set)
        for did in view.device_ids():
            hist = view.history(did)
            runs = _status_runs(hist, chk)
            if not runs:
                continue
            for s, st in runs:
                by_date_all[s.date].add(did)
                if st == "fail":
                    by_date_fail[s.date].add(did)
            statuses = [st for _, st in runs]
            transitions = sum(1 for a, b in zip(statuses, statuses[1:]) if a != b)
            first_s, first_st = runs[0]
            last_s, last_st = runs[-1]
            if all(st == "fail" for st in statuses):
                chronic.append((did, first_s, last_s))
                continue
            if first_st == "pass" and last_st == "fail":
                # start of the final failing run
                idx = max(i for i, st in enumerate(statuses) if st == "pass") + 1
                fail_start, last_pass = runs[idx][0], runs[idx - 1][0]
                fail_days = len(statuses) - idx
                out.append(Insight(
                    category="compliance_drift", severity=sev if sev in SEVERITY_RANK else "medium",
                    title=f"Compliance drift: {chk} regressed to failing",
                    finding=(f"{did}: '{chk}' ({sev} severity) passed until {last_pass.date}, has failed since {fail_start.date} "
                             f"and is still failing on {last_s.date} ({fail_days} consecutive failing snapshots)."),
                    explanation=("The device was compliant at the start of the window and drifted out of compliance; the failure has "
                                 "persisted rather than self-correcting, so it needs remediation."
                                 + (f" Status changed {transitions} times in the window." if transitions > 1 else "")),
                    device_id=did, employee_id=hist[-1].employee_id,
                    metrics={"check_id": chk, "severity": sev, "last_pass": last_pass.date, "failing_since": fail_start.date,
                             "consecutive_failing_snapshots": fail_days, "transitions": transitions},
                    evidence=[
                        EvidenceSpec(claim=f"{did} '{chk}' status 'pass' on {last_pass.date}", device_id=did, snapshots=[last_pass],
                                     fields={f"compliance.{chk}.status": "pass"}),
                        EvidenceSpec(claim=f"{did} '{chk}' status 'fail' on {fail_start.date} (first failing snapshot)", device_id=did,
                                     snapshots=[fail_start], fields={f"compliance.{chk}.status": "fail"}),
                        EvidenceSpec(claim=f"{did} '{chk}' status 'fail' on {last_s.date} (latest snapshot)", device_id=did,
                                     snapshots=[last_s], fields={f"compliance.{chk}.status": "fail", f"compliance.{chk}.severity": sev}),
                    ],
                    recommended_actions=[{"tool": "open_remediation_ticket", "args": {
                        "device_id": did, "check_id": chk,
                        "note": f"'{chk}' has failed since {fail_start.date} (passed until {last_pass.date}); restore compliance."}}],
                ))
            elif first_st == "fail" and last_st == "pass":
                out.append(Insight(
                    category="compliance_drift", severity="info", title=f"Compliance recovered: {chk}",
                    finding=f"{did}: '{chk}' was failing on {first_s.date} and is passing on {last_s.date}.",
                    explanation="The device returned to compliance during the window; no action needed.",
                    device_id=did, employee_id=hist[-1].employee_id, metrics={"check_id": chk, "transitions": transitions},
                    evidence=[EvidenceSpec(claim=f"{did} '{chk}' status 'pass' on {last_s.date}", device_id=did, snapshots=[last_s],
                                           fields={f"compliance.{chk}.status": "pass"})],
                ))
        dates = sorted(by_date_all)
        if chronic:
            devs = [d for d, _, _ in chronic]
            snaps = [s for _, a, b in chronic for s in (a, b)]
            out.append(Insight(
                category="compliance_drift", severity=sev if sev in SEVERITY_RANK else "medium",
                title=f"Chronic non-compliance: {chk}",
                finding=(f"'{chk}' ({sev} severity) failed in every snapshot from {dates[0]} to {dates[-1]} on "
                         f"{len(devs)} of {len(view.device_ids())} devices: {', '.join(devs)}."),
                explanation="This is a persistent gap rather than drift: these devices never passed the check during the window.",
                metrics={"check_id": chk, "severity": sev, "devices_failing_entire_window": len(devs)}, devices=devs,
                evidence=[EvidenceSpec(
                    claim=f"'{chk}' failed in all snapshots {dates[0]} to {dates[-1]} for {len(devs)} devices: {', '.join(devs)}",
                    snapshots=snaps, fields={"check_id": chk, "devices": devs}, period=[dates[0], dates[-1]])],
                recommended_actions=[{"tool": "open_remediation_ticket", "args": {
                    "device_id": d, "check_id": chk, "note": f"'{chk}' failing in every snapshot since {dates[0]}."}} for d in devs],
            ))
        if dates:
            n0, n1 = len(by_date_fail.get(dates[0], ())), len(by_date_fail.get(dates[-1], ()))
            t0, t1 = len(by_date_all[dates[0]]), len(by_date_all[dates[-1]])
            if n1 > n0:
                series = {d: len(by_date_fail.get(d, ())) for d in dates}
                last_day_snaps = [view.latest(d) for d in sorted(by_date_fail.get(dates[-1], ()))]
                out.append(Insight(
                    category="compliance_drift", severity=sev if sev in SEVERITY_RANK else "medium",
                    title=f"Fleet compliance drift: {chk}",
                    finding=(f"'{chk}' failures rose from {n0} of {t0} devices on {dates[0]} to {n1} of {t1} devices on {dates[-1]}."),
                    explanation="The number of non-compliant devices grew over the window, i.e. the fleet is drifting out of compliance on this control.",
                    metrics={"check_id": chk, "failing_first_day": n0, "failing_last_day": n1, "daily_failing_counts": series},
                    devices=sorted(by_date_fail.get(dates[-1], ())),
                    evidence=[EvidenceSpec(
                        claim=f"'{chk}' failing devices per day rose from {n0}/{t0} ({dates[0]}) to {n1}/{t1} ({dates[-1]})",
                        snapshots=last_day_snaps, fields={"daily_failing_counts": series}, period=[dates[0], dates[-1]])],
                ))
    return _sort(out)


# --------------------------------------------------------------------------- software
def detect_software_changes(view: TenantView, th: Thresholds) -> list[Insight]:
    out: list[Insight] = []
    devices = view.device_ids()
    first_sets = [{s.name for s in view.history(d)[0].software} for d in devices]
    baseline = sorted(set.intersection(*first_sets)) if first_sets else []
    review = dict(th.software_review_list)
    for did in devices:
        hist = view.history(did)
        initial = {s.name for s in hist[0].software}
        latest = hist[-1]
        seen_new: dict[str, int] = {}
        for i, s in enumerate(hist):
            for sw in s.software:
                if sw.name not in initial and sw.name not in seen_new:
                    seen_new[sw.name] = i
        for name, idx in sorted(seen_new.items()):
            first_s, prev_s = hist[idx], hist[idx - 1]
            sw = first_s.has_software(name)
            still = latest.has_software(name) is not None
            reason = next((why for key, why in review.items() if key in name.lower()), None)
            severity = "high" if reason else "medium"
            label = f"{name} {sw.version}" if sw and sw.version else name
            finding = (f"{did}: '{label}' first appeared on {first_s.date} (absent on {prev_s.date}) and is "
                       f"{'still installed' if still else 'no longer installed'} as of {latest.date}.")
            explanation = (f"'{name}' is not part of this fleet's common software baseline ({', '.join(baseline)}). "
                           + (f"It matches the configured software review list ({reason}), so it warrants review." if reason
                              else "It was installed during the observation window; confirm it is approved."))
            ev = [
                EvidenceSpec(claim=f"{did} '{name}' not installed on {prev_s.date}", device_id=did, snapshots=[prev_s],
                             fields={"installed_software.contains": False, "software": name}),
                EvidenceSpec(claim=f"{did} '{name}' installed on {first_s.date}", device_id=did, snapshots=[first_s],
                             fields={"installed_software.contains": True, "software": name, "version": sw.version if sw else None}),
            ]
            if latest is not first_s:
                ev.append(EvidenceSpec(claim=f"{did} '{name}' {'installed' if still else 'not installed'} on {latest.date} (latest)",
                                       device_id=did, snapshots=[latest], fields={"installed_software.contains": still, "software": name}))
            actions = []
            if latest.employee_id and still:
                actions.append({"tool": "notify_employee", "args": {"employee_id": latest.employee_id, "message": (
                    f"'{name}' was installed on your device {did} on {first_s.date}. It is not part of the standard software set; "
                    "please confirm it is approved for business use or uninstall it.")}})
            out.append(Insight(
                category="software_change", severity=severity, title=f"New non-baseline software: {name}",
                finding=finding, explanation=explanation, device_id=did, employee_id=latest.employee_id,
                metrics={"software": name, "first_seen": first_s.date, "still_installed": still, "review_list_reason": reason},
                evidence=ev, recommended_actions=actions,
            ))
    return _sort(out)


# --------------------------------------------------------------------------- telemetry gaps
def detect_telemetry_gaps(view: TenantView, th: Thresholds) -> list[Insight]:
    out: list[Insight] = []
    for did in view.device_ids():
        hist = view.history(did)
        prof = view.profile(did)
        issues, evidence = [], []
        if prof.device_type == "laptop":
            missing = [s for s in hist if s.battery is None]
            if missing:
                issues.append(f"battery section missing in {len(missing)}/{len(hist)} snapshots ({', '.join(s.date for s in missing)})")
                evidence.append(EvidenceSpec(claim=f"{did} snapshots without battery data: {', '.join(s.date for s in missing)}",
                                             device_id=did, snapshots=missing, fields={"battery": None}))
        no_net = [s for s in hist if not s.network_reported]
        if no_net:
            issues.append(f"network section missing in {len(no_net)}/{len(hist)} snapshots")
            evidence.append(EvidenceSpec(claim=f"{did} snapshots without network data: {', '.join(s.date for s in no_net)}",
                                         device_id=did, snapshots=no_net, fields={"network": None}))
        cyc = [(s, s.battery.cycle_count) for s in hist if s.battery and s.battery.cycle_count is not None]
        drops = [(a, b) for a, b in zip(cyc, cyc[1:]) if b[1] < a[1]]
        if drops:
            issues.append(f"battery cycle_count decreased between consecutive snapshots {len(drops)} times")
            a, b = drops[0]
            evidence.append(EvidenceSpec(claim=f"{did} cycle_count went from {a[1]} ({a[0].date}) to {b[1]} ({b[0].date})",
                                         device_id=did, snapshots=[a[0], b[0]], fields={"battery.cycle_count": [a[1], b[1]]}))
        if issues:
            out.append(Insight(
                category="telemetry_gap", severity="info", title="Telemetry data-quality gaps",
                finding=f"{did}: " + "; ".join(issues) + ".",
                explanation="Conclusions for this device use the last available reading for affected fields; cycle counts are treated as approximate.",
                device_id=did, employee_id=hist[-1].employee_id, metrics={"issues": issues}, evidence=evidence,
            ))
    return _sort(out)


DETECTORS = {
    "battery_health": detect_battery_health,
    "memory_pressure": detect_memory_pressure,
    "storage_pressure": detect_storage_pressure,
    "compliance_drift": detect_compliance_drift,
    "software_change": detect_software_changes,
    "telemetry_gap": detect_telemetry_gaps,
}


def run_all(view: TenantView, th: Thresholds, categories: list[str] | None = None) -> list[Insight]:
    out: list[Insight] = []
    for cat in categories or list(DETECTORS):
        out.extend(DETECTORS[cat](view, th))
    return _sort(out)
