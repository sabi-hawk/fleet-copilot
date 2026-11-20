"""Insight & trend tools: thin wrappers that register detector evidence in the ledger."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.analytics.insights import (
    SEVERITY_RANK,
    Insight,
    detect_battery_health,
    detect_compliance_drift,
    detect_memory_pressure,
    detect_software_changes,
    detect_storage_pressure,
    detect_telemetry_gaps,
    run_all,
)
from fleet_copilot.tools.base import ToolContext, ToolOutput, ToolSpec


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _render(ctx: ToolContext, tool: str, insights: list[Insight]) -> ToolOutput:
    out, ev_all = [], []
    for ins in insights:
        ev = [ctx.ledger.add_spec(tool, spec) for spec in ins.evidence]
        ev_all.extend(ev)
        out.append({
            "insight_id": ins.insight_id, "category": ins.category, "severity": ins.severity, "title": ins.title,
            "device_id": ins.device_id, "employee_id": ins.employee_id, "devices": ins.devices or None,
            "finding": ins.finding, "explanation": ins.explanation, "evidence_ids": ev, "metrics": ins.metrics,
            "recommended_actions": ins.recommended_actions,
        })
    if not insights:
        v = ctx.view
        eid = ctx.ledger.add(tool=tool, claim=f"{tool} found no matching patterns across {len(v.device_ids())} devices ({v.window_label})",
                             snapshots=v.latest_all(), fields={"insights": 0})
        ev_all.append(eid)
    counts = {}
    for i in insights:
        counts[i.severity] = counts.get(i.severity, 0) + 1
    summary = f"{len(insights)} insight(s)" + (f" by severity {counts}" if counts else "") + f"; telemetry window {ctx.view.window_label}."
    return ToolOutput(summary, {"insights": out, "window": ctx.view.window_label, "thresholds": _thresholds(ctx)}, list(dict.fromkeys(ev_all)))


def _thresholds(ctx: ToolContext) -> dict:
    th = ctx.settings.thresholds
    return {"low_disk_free_pct": th.low_disk_free_pct, "critical_disk_free_pct": th.critical_disk_free_pct,
            "high_memory_used_pct": th.high_memory_used_pct, "memory_sustained_share": th.memory_sustained_share,
            "battery_fade_alert_pct": th.battery_fade_alert_pct}


class NoArgs(_Args):
    pass


class DriftArgs(_Args):
    check_id: str | None = Field(None, description="Restrict to one compliance check; omit for all checks.")


class ScanArgs(_Args):
    categories: list[Literal["battery_health", "memory_pressure", "storage_pressure", "compliance_drift", "software_change", "telemetry_gap"]] | None = Field(
        None, description="Detectors to run; omit for all except telemetry_gap.")
    min_severity: Literal["critical", "high", "medium", "low", "info"] = Field("low", description="Drop insights below this severity.")


def _battery(ctx, _a):
    return _render(ctx, "detect_battery_health", detect_battery_health(ctx.view, ctx.settings.thresholds))


def _memory(ctx, _a):
    return _render(ctx, "detect_memory_pressure", detect_memory_pressure(ctx.view, ctx.settings.thresholds))


def _storage(ctx, _a):
    return _render(ctx, "detect_storage_pressure", detect_storage_pressure(ctx.view, ctx.settings.thresholds))


def _drift(ctx, a: DriftArgs):
    if a.check_id and a.check_id not in ctx.view.check_catalog():
        raise ValueError(f"unknown check_id '{a.check_id}'; known: {', '.join(ctx.view.check_catalog())}")
    return _render(ctx, "detect_compliance_drift", detect_compliance_drift(ctx.view, ctx.settings.thresholds, a.check_id))


def _software(ctx, _a):
    return _render(ctx, "detect_software_changes", detect_software_changes(ctx.view, ctx.settings.thresholds))


def _gaps(ctx, _a):
    return _render(ctx, "get_data_quality_report", detect_telemetry_gaps(ctx.view, ctx.settings.thresholds))


def _scan(ctx, a: ScanArgs):
    cats = a.categories or ["battery_health", "memory_pressure", "storage_pressure", "compliance_drift", "software_change"]
    floor = SEVERITY_RANK[a.min_severity]
    found = [i for i in run_all(ctx.view, ctx.settings.thresholds, cats) if SEVERITY_RANK.get(i.severity, 9) <= floor]
    return _render(ctx, "run_fleet_health_scan", found)


INSIGHT_TOOLS = [
    ToolSpec("detect_battery_health",
             "Batteries approaching end-of-life: OS battery condition leaving 'Normal' and/or full-charge capacity fading over the window.",
             NoArgs, "insight", _battery),
    ToolSpec("detect_memory_pressure",
             "Devices consistently (>=50% of snapshots) or intermittently (>=25%) at or above 85% RAM utilisation.",
             NoArgs, "insight", _memory),
    ToolSpec("detect_storage_pressure",
             "Devices low on storage, consistently below the low-disk threshold, or trending toward exhaustion (fitted GB/day trend).",
             NoArgs, "insight", _storage),
    ToolSpec("detect_compliance_drift",
             "Compliance drift over time: devices that regressed from pass to fail, chronic failures, and fleet-level growth in failures.",
             DriftArgs, "insight", _drift),
    ToolSpec("detect_software_changes",
             "Applications that appeared during the window and are not in the fleet's common baseline (flags configured review-list apps).",
             NoArgs, "insight", _software),
    ToolSpec("get_data_quality_report",
             "Telemetry gaps and anomalies (missing battery/network sections on laptops, non-monotonic cycle counts) that limit conclusions.",
             NoArgs, "insight", _gaps),
    ToolSpec("run_fleet_health_scan",
             "Run several detectors at once and return a severity-ranked list of insights, each with finding, evidence and explanation.",
             ScanArgs, "insight", _scan),
]
