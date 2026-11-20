"""Action tools. They only create proposals; execution needs policy approval and a human decision."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.tools.base import ToolContext, ToolOutput, ToolSpec


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateUpgradeOrderArgs(_Args):
    device_id: str = Field(..., description="Device to upgrade.")
    component: str = Field(..., description="'memory', 'storage' or 'battery'.")
    spec: str = Field(..., description="Target spec, e.g. '32 GB' (memory), '1 TB' (storage), 'replacement battery (OEM)'.")


class OpenRemediationTicketArgs(_Args):
    device_id: str = Field(..., description="Device that fails the check.")
    check_id: str = Field(..., description="Compliance check ID, e.g. 'screen_lock'.")
    note: str = Field(..., description="What is wrong and what to fix, citing the telemetry finding.")


class FlagDeviceForReplacementArgs(_Args):
    device_id: str = Field(..., description="Device to flag.")
    reason: str = Field(..., description="Why the device should be replaced, citing the telemetry finding.")


class NotifyEmployeeArgs(_Args):
    employee_id: str = Field(..., description="Employee to notify.")
    message: str = Field(..., description="Plain-language message (max 1000 chars). No credentials, no other employees' data.")


def _propose(ctx: ToolContext, tool: str, args: dict) -> ToolOutput:
    for p in ctx.proposals:
        if p["tool"] == tool and p["args"] == args:
            return ToolOutput(f"Proposal {p['proposal_id']} for this exact action already exists in this turn.",
                              {"proposal_id": p["proposal_id"], "status": p["status"]}, p["policy"]["evidence_ids"], p)
    pid = f"P{ctx.turn}-{len(ctx.proposals) + 1}"
    target_ev = _register_target(ctx, tool, args)
    if len(ctx.proposals) >= ctx.settings.max_proposals_per_turn:
        decision = {"allowed": False, "rule": "bulk_action_limit",
                    "reasons": [f"more than {ctx.settings.max_proposals_per_turn} actions proposed in one turn; narrow the request"],
                    "signals": [], "evidence_ids": []}
    else:
        decision = ctx.policy.evaluate(tool, args).to_dict()
    proposal = {
        "proposal_id": pid, "tool": tool, "args": args, "session_id": ctx.session_id, "turn": ctx.turn,
        "requested_by": "agent", "policy": decision,
        "status": "pending_approval" if decision["allowed"] else "rejected_by_policy",
    }
    ctx.proposals.append(proposal)
    if decision["allowed"]:
        summary = (f"Proposal {pid} recorded: {tool}({args}). Policy check PASSED ({'; '.join(decision['reasons'])}). "
                   "It has NOT been executed: it now waits for explicit approval by an IT administrator. Do not say it was done.")
    else:
        summary = (f"Proposal {pid} REJECTED by policy ({decision['rule']}): {'; '.join(decision['reasons'])}. "
                   "Tell the user why; do not retry the same action without new evidence.")
    data = {"proposal_id": pid, "status": proposal["status"], "policy_rule": decision["rule"], "reasons": decision["reasons"],
            "supporting_evidence": [{"evidence_id": s["evidence_id"], "claim": s["claim"]} for s in decision["signals"]]}
    return ToolOutput(summary, data, target_ev + [e for e in decision["evidence_ids"] if e not in target_ev], proposal)


def _register_target(ctx: ToolContext, tool: str, args: dict) -> list[str]:
    """Evidence that the target exists in this tenant. It never justifies an action on its own."""
    v = ctx.view
    if args.get("device_id") and v.has_device(args["device_id"]):
        s = v.latest(args["device_id"])
        return [ctx.ledger.add(tool=tool, claim=f"{s.device_id} is a {v.device_type(s.device_id)} ({s.model_name}) assigned to "
                                                f"{s.employee_id}, last reported {s.date}",
                               device_id=s.device_id, snapshots=[s], fields={"employee_id": s.employee_id})]
    if args.get("employee_id") and v.has_employee(args["employee_id"]):
        devs = v.employee_devices(args["employee_id"])
        return [ctx.ledger.add(tool=tool, claim=f"{args['employee_id']} is assigned {len(devs)} device(s): {', '.join(devs)}",
                               snapshots=[v.latest(d) for d in devs], fields={"employee_id": args["employee_id"]})]
    return []


def _upgrade(ctx, a: CreateUpgradeOrderArgs):
    ctx.view.require_device(a.device_id)
    return _propose(ctx, "create_upgrade_order", a.model_dump())


def _ticket(ctx, a: OpenRemediationTicketArgs):
    ctx.view.require_device(a.device_id)
    return _propose(ctx, "open_remediation_ticket", a.model_dump())


def _replace(ctx, a: FlagDeviceForReplacementArgs):
    ctx.view.require_device(a.device_id)
    return _propose(ctx, "flag_device_for_replacement", a.model_dump())


def _notify(ctx, a: NotifyEmployeeArgs):
    ctx.view.require_employee(a.employee_id)
    return _propose(ctx, "notify_employee", a.model_dump())


_NOTE = " Creates a PROPOSAL only; a policy check and explicit human approval are required before anything is executed."
ACTION_TOOL_SPECS = [
    ToolSpec("create_upgrade_order",
             "Propose a hardware upgrade order (memory, storage or battery) for a device. Requires telemetry evidence of sustained "
             "need (e.g. sustained RAM pressure, low/declining storage, degraded battery) and a spec larger than what is installed." + _NOTE,
             CreateUpgradeOrderArgs, "action", _upgrade),
    ToolSpec("open_remediation_ticket",
             "Propose an IT remediation ticket for a compliance check that is failing on the device's latest snapshot." + _NOTE,
             OpenRemediationTicketArgs, "action", _ticket),
    ToolSpec("flag_device_for_replacement",
             "Propose flagging a device for replacement. Requires a replacement-grade signal: battery end-of-life or sustained RAM "
             "saturation." + _NOTE, FlagDeviceForReplacementArgs, "action", _replace),
    ToolSpec("notify_employee",
             "Propose sending a message to an employee about an issue on one of their devices." + _NOTE,
             NotifyEmployeeArgs, "action", _notify),
]
