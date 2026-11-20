"""Action outbox: the (simulated) downstream systems that approved actions write to.

The assessment's action tools are state-changing. Here, "executing" an action
means writing an idempotent record into a tenant-partitioned JSON outbox
(tickets, upgrade orders, replacement flags, employee notifications) that a
real integration (ServiceNow, procurement, email) would consume. Nothing is
ever written here without a policy pass AND an explicit human approval; the
graph enforces that ordering and `execute` re-checks the approval record.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COLLECTIONS = {
    "create_upgrade_order": ("upgrade_orders", "ORD"),
    "open_remediation_ticket": ("remediation_tickets", "TKT"),
    "flag_device_for_replacement": ("replacement_flags", "RPL"),
    "notify_employee": ("employee_notifications", "MSG"),
}


class ApprovalMissing(PermissionError):
    pass


def idempotency_key(tenant_id: str, tool: str, args: dict[str, Any]) -> str:
    payload = json.dumps({"tenant": tenant_id, "tool": tool, "args": args}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ActionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {name: [] for name, _ in COLLECTIONS.values()}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for name, _ in COLLECTIONS.values():
            data.setdefault(name, [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def records(self, tenant_id: str, tool: str | None = None) -> list[dict[str, Any]]:
        data = self._load()
        names = [COLLECTIONS[tool][0]] if tool else [n for n, _ in COLLECTIONS.values()]
        return [r for n in names for r in data[n] if r["tenant_id"] == tenant_id]

    def find_existing(self, tenant_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        key = idempotency_key(tenant_id, tool, args)
        for r in self.records(tenant_id, tool):
            if r["idempotency_key"] == key:
                return r
        return None

    def has_open_ticket(self, tenant_id: str, device_id: str, check_id: str) -> dict[str, Any] | None:
        for r in self.records(tenant_id, "open_remediation_ticket"):
            if r["args"]["device_id"] == device_id and r["args"]["check_id"] == check_id and r["status"] == "open":
                return r
        return None

    def has_replacement_flag(self, tenant_id: str, device_id: str) -> dict[str, Any] | None:
        for r in self.records(tenant_id, "flag_device_for_replacement"):
            if r["args"]["device_id"] == device_id:
                return r
        return None

    def execute(self, *, tenant_id: str, proposal: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
        if not approval or approval.get("decision") != "approve" or not approval.get("approver"):
            raise ApprovalMissing(f"Refusing to execute {proposal.get('proposal_id')}: no explicit human approval recorded")
        tool, args = proposal["tool"], proposal["args"]
        collection, prefix = COLLECTIONS[tool]
        with self._lock:
            data = self._load()
            key = idempotency_key(tenant_id, tool, args)
            for r in data[collection]:
                if r["tenant_id"] == tenant_id and r["idempotency_key"] == key:
                    return {**r, "duplicate": True}
            seq = len(data[collection]) + 1
            record = {
                "id": f"{prefix}-{seq:04d}",
                "tenant_id": tenant_id,
                "tool": tool,
                "args": args,
                "status": "open" if tool == "open_remediation_ticket" else ("queued" if tool == "notify_employee" else "submitted"),
                "proposal_id": proposal.get("proposal_id"),
                "session_id": proposal.get("session_id"),
                "evidence_ids": proposal.get("policy", {}).get("evidence_ids", []),
                "justification": [s["claim"] for s in proposal.get("policy", {}).get("signals", [])],
                "approved_by": approval["approver"],
                "approved_at": approval.get("at"),
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "idempotency_key": key,
            }
            data[collection].append(record)
            self._save(data)
            return record
