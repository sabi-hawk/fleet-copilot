"""Evidence requirements for state-changing actions."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from fleet_copilot.actions import ActionStore
from fleet_copilot.analytics.insights import memory_profile, storage_profile
from fleet_copilot.config import Thresholds
from fleet_copilot.data.store import NotFoundInTenant, TenantView
from fleet_copilot.evidence import EvidenceLedger

ACTION_TOOLS = ("create_upgrade_order", "open_remediation_ticket", "flag_device_for_replacement", "notify_employee")
_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(tb|gb)\b", re.IGNORECASE)
_SECRET_RE = re.compile(r"(password|passwd|passcode|secret|api[\s_-]?key|access[\s_-]?token|bearer\s+[a-z0-9])", re.IGNORECASE)
COMPONENT_ALIASES = {
    "memory": "memory", "ram": "memory",
    "storage": "storage", "disk": "storage", "ssd": "storage", "drive": "storage",
    "battery": "battery",
}
MAX_MESSAGE_CHARS = 1000


@dataclass
class PolicyDecision:
    allowed: bool
    rule: str
    reasons: list[str] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def evidence_ids(self) -> list[str]:
        return [s["evidence_id"] for s in self.signals if s.get("evidence_id")]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence_ids"] = self.evidence_ids
        return d


def parse_capacity_gb(spec: str) -> float | None:
    m = _CAPACITY_RE.search(spec or "")
    if not m:
        return None
    value = float(m.group(1))
    return value * 1024 if m.group(2).lower() == "tb" else value


class ActionPolicyEngine:
    TOOL = "policy_engine"

    def __init__(self, view: TenantView, ledger: EvidenceLedger, thresholds: Thresholds, store: ActionStore, foreign_identifiers: set[str] | None = None):
        self.view = view
        self.ledger = ledger
        self.th = thresholds
        self.store = store
        self.foreign = foreign_identifiers or set()
    def _signal(self, name: str, claim: str, device_id: str | None, snapshots: list, fields: dict | None = None, period: list | None = None) -> dict[str, Any]:
        eid = self.ledger.add(tool=self.TOOL, claim=claim, device_id=device_id, snapshots=snapshots, fields=fields or {}, period=period)
        return {"name": name, "claim": claim, "evidence_id": eid}

    def _deny(self, rule: str, *reasons: str, signals: list | None = None) -> PolicyDecision:
        return PolicyDecision(False, rule, list(reasons), signals or [])

    # Signal helpers return (positive_signal | None, observed_signal) so rejections are evidenced too.
    def _battery_signal(self, did: str) -> tuple[dict | None, dict]:
        hist = self.view.history(did)
        bs = [s for s in hist if s.battery and s.battery.full_charge_capacity]
        if len(bs) < 2:
            return None, self._signal("no_battery_telemetry", f"{did} reports no battery telemetry ({len(hist)} snapshots)", did,
                                      [hist[-1]], {"battery": None})
        first, last = bs[0], bs[-1]
        fade = (first.battery.full_charge_capacity - last.battery.full_charge_capacity) / first.battery.full_charge_capacity * 100
        cond = last.battery.condition or "unknown"
        positive = cond.lower() != "normal" or fade >= self.th.battery_fade_alert_pct
        sig = self._signal(
            "battery_end_of_life" if positive else "battery_healthy",
            f"{did} battery condition '{cond}' on {last.date}; full_charge_capacity {first.battery.full_charge_capacity} -> "
            f"{last.battery.full_charge_capacity} ({fade:.1f}% fade since {first.date})",
            did, [first, last], {"battery.condition": cond, "fade_pct": round(fade, 1)},
        )
        return (sig if positive else None), sig

    def _memory_signal(self, did: str) -> tuple[dict | None, dict, dict]:
        hist = self.view.history(did)
        p = memory_profile(hist, self.th)
        sig = self._signal(
            "sustained_memory_pressure" if p["sustained"] else "memory_below_sustained_bar",
            f"{did} memory used >= {self.th.high_memory_used_pct:.0f}% in {p['at_or_above']} of {p['snapshots']} snapshots "
            f"from {hist[0].date} to {hist[-1].date} (mean {p['mean_pct']:.1f}%)",
            did, hist, {"snapshots_constrained": p["at_or_above"], "snapshots_total": p["snapshots"]},
            [hist[0].collected_at_iso, hist[-1].collected_at_iso],
        )
        return (sig if p["sustained"] else None), sig, p

    def _storage_signal(self, did: str) -> tuple[dict | None, dict, dict | None]:
        hist = self.view.history(did)
        p = storage_profile(hist, self.th)
        if p is None:
            return None, self._signal("no_disk_telemetry", f"{did} reports no disk telemetry", did, [hist[-1]], {}), None
        ld = p["last_disk"]
        projected = p["days_to_critical"] is not None and p["days_to_critical"] <= self.th.storage_horizon_days and p["slope_gb_per_day"] < 0
        positive = ld.free_pct < self.th.low_disk_free_pct or p["consistent"] or projected
        sig = self._signal(
            "storage_pressure" if positive else "storage_healthy",
            f"{did} free disk {ld.free_pct:.1f}% ({ld.free_gb:.1f} GB of {ld.size_gb:.0f} GB) on {p['last'].date}; "
            f"below {self.th.low_disk_free_pct:.0f}% in {len(p['below'])} of {p['n']} snapshots",
            did, [p["last"]], {"disk.free_pct": round(ld.free_pct, 1)},
        )
        return (sig if positive else None), sig, p
    def evaluate(self, tool: str, args: dict[str, Any]) -> PolicyDecision:
        try:
            if tool == "create_upgrade_order":
                return self._upgrade(args)
            if tool == "open_remediation_ticket":
                return self._ticket(args)
            if tool == "flag_device_for_replacement":
                return self._replacement(args)
            if tool == "notify_employee":
                return self._notify(args)
        except NotFoundInTenant as exc:
            return self._deny("tenant_isolation", str(exc))
        return self._deny("unknown_action", f"'{tool}' is not an allowed action")

    def _upgrade(self, a: dict[str, Any]) -> PolicyDecision:
        rule = "upgrade_requires_sustained_evidence"
        did = a.get("device_id", "")
        self.view.require_device(did)
        comp = COMPONENT_ALIASES.get(str(a.get("component", "")).strip().lower())
        spec = str(a.get("spec", "")).strip()
        if comp is None:
            return self._deny(rule, f"unsupported component '{a.get('component')}' (allowed: memory, storage, battery)")
        if not spec:
            return self._deny(rule, "an upgrade spec is required")
        if (dup := self.store.find_existing(self.view.tenant_id, "create_upgrade_order", a)):
            return self._deny("idempotency", f"identical order already submitted as {dup['id']}")
        last = self.view.latest(did)
        if comp == "memory":
            sig, obs, _ = self._memory_signal(did)
            if not sig:
                return self._deny(rule, f"insufficient evidence for a memory upgrade: {obs['claim']} - below the "
                                        f"{self.th.memory_sustained_share:.0%} sustained-pressure bar", signals=[obs])
            target, current = parse_capacity_gb(spec), last.ram_gb or 0
            if target is None:
                return self._deny(rule, f"spec '{spec}' has no capacity (e.g. '32 GB')", signals=[sig])
            if target <= current:
                return self._deny(rule, f"spec {target:.0f} GB is not larger than installed RAM ({current:.0f} GB)", signals=[sig])
            return PolicyDecision(True, rule, [f"sustained memory pressure; {current:.0f} GB -> {target:.0f} GB"], [sig])
        if comp == "storage":
            sig, obs, p = self._storage_signal(did)
            if not sig:
                return self._deny(rule, f"insufficient evidence for a storage upgrade: {obs['claim']} (threshold "
                                        f"{self.th.low_disk_free_pct:.0f}%, no exhaustion trend)", signals=[obs])
            target, current = parse_capacity_gb(spec), p["last_disk"].size_gb
            if target is None:
                return self._deny(rule, f"spec '{spec}' has no capacity (e.g. '1 TB')", signals=[sig])
            if target <= current + 0.5:
                return self._deny(rule, f"spec {target:.0f} GB is not larger than the current volume ({current:.0f} GB)", signals=[sig])
            return PolicyDecision(True, rule, [f"storage pressure; {current:.0f} GB -> {target:.0f} GB"], [sig])
        sig, obs = self._battery_signal(did)
        if not sig:
            return self._deny(rule, f"insufficient evidence for a battery replacement: {obs['claim']}", signals=[obs])
        return PolicyDecision(True, rule, ["battery degradation signal present"], [sig])

    def _ticket(self, a: dict[str, Any]) -> PolicyDecision:
        rule = "ticket_requires_current_failure"
        did, chk, note = a.get("device_id", ""), str(a.get("check_id", "")), str(a.get("note", "")).strip()
        self.view.require_device(did)
        catalog = self.view.check_catalog()
        if chk not in catalog:
            return self._deny(rule, f"unknown compliance check '{chk}' (known: {', '.join(catalog)})")
        if len(note) < 5:
            return self._deny(rule, "a remediation note is required")
        latest = self.view.latest(did)
        res = latest.compliance_status(chk)
        if res is None:
            return self._deny(rule, f"'{chk}' was not reported in {did}'s latest snapshot ({latest.date})")
        if res.status != "fail":
            sig = self._signal("check_passing", f"{did} '{chk}' status '{res.status}' on {latest.date} (latest snapshot)", did, [latest],
                               {f"compliance.{chk}.status": res.status})
            return self._deny(rule, f"insufficient evidence: '{chk}' is '{res.status}' on {did}'s latest snapshot ({latest.date})", signals=[sig])
        if (dup := self.store.has_open_ticket(self.view.tenant_id, did, chk)):
            return self._deny("idempotency", f"ticket {dup['id']} is already open for {did}/{chk}")
        sig = self._signal("check_failing", f"{did} '{chk}' status 'fail' on {latest.date} (latest snapshot)", did, [latest],
                           {f"compliance.{chk}.status": "fail", f"compliance.{chk}.severity": res.severity})
        return PolicyDecision(True, rule, [f"'{chk}' ({res.severity}) failing on latest snapshot"], [sig])

    def _replacement(self, a: dict[str, Any]) -> PolicyDecision:
        rule = "replacement_requires_replacement_grade_signal"
        did, reason = a.get("device_id", ""), str(a.get("reason", "")).strip()
        self.view.require_device(did)
        if len(reason) < 5:
            return self._deny(rule, "a replacement reason is required")
        if (dup := self.store.has_replacement_flag(self.view.tenant_id, did)):
            return self._deny("idempotency", f"{did} is already flagged for replacement ({dup['id']})")
        bsig, bobs = self._battery_signal(did)
        msig, mobs, _ = self._memory_signal(did)
        signals = [s for s in (bsig, msig) if s]
        if not signals:
            ssig, _, _ = self._storage_signal(did)
            extra = " Storage pressure exists but is remediable (cleanup/upgrade), not replacement-grade." if ssig else ""
            return self._deny(rule, f"insufficient evidence: no replacement-grade signal in telemetry ({bobs['claim']}; "
                                    f"{mobs['claim']})." + extra, signals=[bobs, mobs] + ([ssig] if ssig else []))
        return PolicyDecision(True, rule, [s["name"] for s in signals], signals)

    def _notify(self, a: dict[str, Any]) -> PolicyDecision:
        rule = "notification_requires_device_issue"
        emp, msg = a.get("employee_id", ""), str(a.get("message", "")).strip()
        self.view.require_employee(emp)
        if not msg:
            return self._deny(rule, "message is empty")
        if len(msg) > MAX_MESSAGE_CHARS:
            return self._deny(rule, f"message exceeds {MAX_MESSAGE_CHARS} characters")
        if _SECRET_RE.search(msg):
            return self._deny("content_safety", "message appears to contain or request credentials")
        leaked = sorted(i for i in self.foreign if i and i in msg)
        if leaked:
            return self._deny("tenant_isolation", "message references identifiers outside your organization")
        if (dup := self.store.find_existing(self.view.tenant_id, "notify_employee", a)):
            return self._deny("idempotency", f"identical notification already queued ({dup['id']})")
        signals = []
        for did in self.view.employee_devices(emp):
            latest = self.view.latest(did)
            for c in latest.compliance:
                if c.status == "fail":
                    signals.append(self._signal("check_failing", f"{did} '{c.check_id}' status 'fail' on {latest.date} (latest snapshot)",
                                                did, [latest], {f"compliance.{c.check_id}.status": "fail"}))
            bsig, _ = self._battery_signal(did)
            if bsig:
                signals.append(bsig)
            msig, _, _ = self._memory_signal(did)
            if msig:
                signals.append(msig)
            ssig, _, _ = self._storage_signal(did)
            if ssig:
                signals.append(ssig)
            initial = {s.name for s in self.view.history(did)[0].software}
            for sw in latest.software:
                if sw.name not in initial:
                    signals.append(self._signal("new_software", f"{did} '{sw.name}' installed on {latest.date} (latest), absent on first snapshot",
                                                did, [self.view.history(did)[0], latest], {"software": sw.name}))
        if not signals:
            return self._deny(rule, f"insufficient evidence: no active telemetry-backed issue on {emp}'s devices")
        return PolicyDecision(True, rule, [f"{len(signals)} active issue signal(s) on the employee's devices"], signals)
