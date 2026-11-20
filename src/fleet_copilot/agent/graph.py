"""The Fleet Copilot agent graph (LangGraph StateGraph).

    START
      -> guard_input        deterministic input guard (cross-tenant, injection)  --refused--> compose
      -> plan               reasoner produces an explicit, audited plan
      -> agent <-> tools    explicit agent loop: reasoner emits tool calls, tool node executes them
                            (tenant-scoped, validated, evidence-registering); bounded by max_agent_iterations
      -> reflect            plan-coverage check; may send the agent back once with a reflection note
      -> policy_gate        authoritative re-evaluation of every proposal by the policy engine
      -> human_approval     LangGraph interrupt(): pauses until an administrator approves/rejects
      -> execute_actions    executes approved proposals only (policy re-checked at execution time)
      -> compose            draft answer + system-generated action status section
      -> verify             grounding verifier; on failure: one revision, then evidence-only fallback
      -> finalize           audit + history
    END
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from fleet_copilot.actions import ActionStore, ApprovalMissing
from fleet_copilot.agent.prompts import reflection_message
from fleet_copilot.agent.reasoners.base import Reasoner, ReasonerContext
from fleet_copilot.agent.state import AgentState
from fleet_copilot.audit import AuditLog
from fleet_copilot.config import Settings
from fleet_copilot.data.store import TelemetryStore, TenantView
from fleet_copilot.evidence import EvidenceLedger
from fleet_copilot.guardrails import ActionPolicyEngine, GroundingVerifier, InputGuard
from fleet_copilot.tools.base import ToolContext, ToolRegistry

APPROVE_WORDS = {"approve", "approved", "yes", "y"}
NON_HUMAN_APPROVERS = {"agent", "assistant", "fleet-copilot", "copilot", "system", "llm", "claude"}


@dataclass
class AgentDeps:
    store: TelemetryStore
    view: TenantView
    registry: ToolRegistry
    reasoner: Reasoner
    settings: Settings
    audit: AuditLog
    actions: ActionStore
    guard: InputGuard
    verifier: GroundingVerifier
    session_id: str
    on_security_event: Callable[[str, dict[str, Any]], None]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" if k in ("note", "reason", "message") else f"{k}={v}" for k, v in args.items())


def action_status_section(proposals: list[dict[str, Any]]) -> str:
    if not proposals:
        return ""
    lines = ["", "---", "Action status (system-generated, authoritative):"]
    for p in proposals:
        pol = p["policy"]
        cite = f" [{', '.join(pol['evidence_ids'])}]" if pol.get("evidence_ids") else ""
        head = f"- {p['proposal_id']} {p['tool']}({_fmt_args(p['args'])})"
        st = p["status"]
        if st == "rejected_by_policy":
            lines.append(f"{head}: REJECTED by policy ({pol['rule']}): {'; '.join(pol['reasons'])}{cite}. Not executed.")
        elif st == "pending_approval":
            lines.append(f"{head}: policy passed{cite}; AWAITING administrator approval. Not executed.")
        elif st == "rejected_by_approver":
            who = p.get("approval", {}).get("approver") or "no approver identity supplied"
            lines.append(f"{head}: policy passed{cite}; REJECTED by approver ({who}). Not executed.")
        elif st == "executed":
            ex = p["execution"]
            lines.append(f"{head}: policy passed{cite}; APPROVED by {p['approval']['approver']}; executed as {ex['record_id']}"
                         + (" (already existed; idempotent)" if ex.get("duplicate") else "") + ".")
        elif st == "blocked_at_execution":
            lines.append(f"{head}: APPROVED but BLOCKED at execution time: {p.get('execution_error')}. Not executed.")
        else:
            lines.append(f"{head}: {st}.")
    return "\n".join(lines)


def evidence_only_answer(state: dict[str, Any], ledger: EvidenceLedger) -> str:
    cited = []
    for r in state.get("tool_calls", []):
        for eid in r.get("evidence_ids", []):
            if eid not in cited:
                cited.append(eid)
    lines = ["I could not produce a narrative answer that passed grounding verification, so here is the verified evidence "
             "gathered for this request:"]
    for eid in cited[:25]:
        ev = ledger.get(eid)
        if ev:
            lines.append(f"- {ev.claim} [{eid}]")
    if not cited:
        lines.append("- (no evidence was gathered)")
    return "\n".join(lines)


def build_graph(deps: AgentDeps, checkpointer: Any):
    s = deps.settings
    audit = deps.audit

    def log(event: str, data: dict[str, Any], actor: str = "agent") -> None:
        audit.record(event, tenant_id=deps.view.tenant_id, session_id=deps.session_id, actor=actor, data=data)

    def rctx(state: AgentState) -> ReasonerContext:
        return ReasonerContext(registry=deps.registry, view=deps.view, settings=s, ledger=EvidenceLedger(state.get("evidence", [])))

    def policy_engine(ledger: EvidenceLedger) -> ActionPolicyEngine:
        return ActionPolicyEngine(deps.view, ledger, s.thresholds, deps.actions, deps.guard.foreign_identifiers)

    # ------------------------------------------------------------------ nodes
    def guard_input(state: AgentState) -> dict:
        d = deps.guard.check(state["question"])
        log("input_guard", {"turn": state["turn"], "question": state["question"], **d.to_dict()}, actor="guard")
        if not d.allowed:
            log("security_event", {"type": f"input_refused:{d.category}", "matches": d.matches}, actor="guard")
        return {"guard": d.to_dict(), "status": "running" if d.allowed else "refused"}

    def plan(state: AgentState) -> dict:
        res = deps.reasoner.plan(state, rctx(state))
        log("plan", {"turn": state["turn"], "reasoner": deps.reasoner.name, "plan": res.plan})
        return {"plan": res.plan, "transcript": state.get("transcript", []) + res.transcript_add,
                "reasoner_state": res.reasoner_state}

    def agent(state: AgentState) -> dict:
        force = state.get("iterations", 0) >= s.max_agent_iterations
        rs_in = dict(state.get("reasoner_state") or {})
        note = rs_in.pop("pending_note", None)
        res = deps.reasoner.act({**state, "reasoner_state": rs_in}, rctx(state), force_final=force, note=note)
        rs = {**rs_in, **res.reasoner_state}
        log("agent_step", {"turn": state["turn"], "iteration": state.get("iterations", 0) + 1, "stop_reason": res.stop_reason,
                           "tool_calls": [{"name": c["name"], "args": c["args"]} for c in res.tool_calls],
                           "forced_final": force, "has_draft": res.draft is not None})
        out = {"pending_tool_calls": res.tool_calls, "iterations": state.get("iterations", 0) + 1, "reasoner_state": rs,
               "transcript": state.get("transcript", []) + res.transcript_add, "stop_reason": res.stop_reason}
        if res.draft is not None:
            out["draft"] = res.draft
        return out

    def tools(state: AgentState) -> dict:
        ledger = EvidenceLedger(state.get("evidence", []))
        proposals = [dict(p) for p in state.get("proposals", [])]
        before = {p["proposal_id"] for p in proposals}
        ctx = ToolContext(view=deps.view, ledger=ledger, settings=s, policy=policy_engine(ledger), session_id=deps.session_id,
                          turn=state["turn"], proposals=proposals, on_security_event=deps.on_security_event)
        records = []
        for call in state.get("pending_tool_calls", []):
            rec = deps.registry.execute(call["id"], call["name"], call.get("args") or {}, ctx)
            records.append(rec)
            log("tool_call", {"turn": state["turn"], "tool": rec["name"], "args": rec["args"], "ok": rec["ok"],
                              "error": rec["error"], "summary": rec["summary"][:500], "evidence_ids": rec["evidence_ids"]})
        for p in proposals:
            if p["proposal_id"] not in before:
                log("action_proposed", {"proposal_id": p["proposal_id"], "tool": p["tool"], "args": p["args"],
                                        "policy": p["policy"], "status": p["status"]})
        return {"tool_calls": state.get("tool_calls", []) + records, "pending_tool_calls": [], "proposals": proposals,
                "evidence": ledger.to_list(), "transcript": state.get("transcript", []) + deps.reasoner.observe(records)}

    def reflect(state: AgentState) -> dict:
        plan_ = state.get("plan") or {}
        called = {r["name"] for r in state.get("tool_calls", [])}
        action_tools = set(deps.registry.names("action"))
        missing = [st for st in plan_.get("steps", [])
                   if st.get("tools") and not (set(st["tools"]) & called) and not set(st["tools"]) <= action_tools]
        budget_left = state.get("iterations", 0) < s.max_agent_iterations
        retry = bool(missing) and state.get("reflections", 0) < s.max_reflections and budget_left and deps.reasoner.name != "rule_based"
        no_draft = state.get("draft") is None
        log("reflection", {"turn": state["turn"], "unexecuted_steps": [m.get("description") for m in missing],
                           "retry": retry, "draft_present": not no_draft, "tool_calls": len(state.get("tool_calls", []))})
        if retry:
            rs = dict(state.get("reasoner_state") or {})
            rs["pending_note"] = reflection_message(missing)
            return {"reflections": state.get("reflections", 0) + 1, "reasoner_state": rs, "draft": None}
        return {"reflections": state.get("reflections", 0)}

    def policy_gate(state: AgentState) -> dict:
        ledger = EvidenceLedger(state.get("evidence", []))
        engine = policy_engine(ledger)
        proposals = [dict(p) for p in state.get("proposals", [])]
        for p in proposals:
            if p["status"] != "pending_approval":
                continue
            decision = engine.evaluate(p["tool"], p["args"]).to_dict()
            if not decision["allowed"]:
                p["policy"], p["status"] = decision, "rejected_by_policy"
            log("policy_decision", {"proposal_id": p["proposal_id"], "tool": p["tool"], "args": p["args"],
                                    "allowed": decision["allowed"], "rule": decision["rule"], "reasons": decision["reasons"],
                                    "evidence_ids": decision["evidence_ids"]}, actor="policy_engine")
        pending = [p["proposal_id"] for p in proposals if p["status"] == "pending_approval"]
        if pending:
            log("approval_requested", {"proposal_ids": pending}, actor="policy_engine")
        return {"proposals": proposals, "evidence": ledger.to_list()}

    def human_approval(state: AgentState) -> dict:
        ledger = EvidenceLedger(state.get("evidence", []))
        pending = [p for p in state.get("proposals", []) if p["status"] == "pending_approval"]
        request = {
            "type": "approval_request", "session_id": deps.session_id, "tenant_id": deps.view.tenant_id,
            "instructions": "Approve or reject each proposal. Unlisted proposals are rejected. An approver identity is required.",
            "proposals": [{"proposal_id": p["proposal_id"], "tool": p["tool"], "args": p["args"],
                           "policy_reasons": p["policy"]["reasons"],
                           "evidence": [{"id": e, "claim": ev.claim} for e in p["policy"]["evidence_ids"] if (ev := ledger.get(e))]}
                          for p in pending],
        }
        response = interrupt(request) or {}
        approver = str(response.get("approver") or "").strip()
        decisions = response.get("decisions") or {}
        valid_approver = bool(approver) and approver.lower() not in NON_HUMAN_APPROVERS
        at = _now()
        proposals = []
        for p in state.get("proposals", []):
            p = dict(p)
            if p["status"] == "pending_approval":
                raw = str(decisions.get(p["proposal_id"], "reject")).lower()
                approve = valid_approver and raw in APPROVE_WORDS
                reason = None if valid_approver else "no valid human approver identity supplied"
                p["approval"] = {"decision": "approve" if approve else "reject", "approver": approver or None, "at": at,
                                 "reason": reason}
                p["status"] = "approved" if approve else "rejected_by_approver"
                log("approval_decision", {"proposal_id": p["proposal_id"], "decision": p["approval"]["decision"],
                                          "approver": approver or None, "reason": reason}, actor=approver or "unknown")
            proposals.append(p)
        return {"proposals": proposals}

    def execute_actions(state: AgentState) -> dict:
        ledger = EvidenceLedger(state.get("evidence", []))
        engine = policy_engine(ledger)
        proposals = []
        for p in state.get("proposals", []):
            p = dict(p)
            if p["status"] == "approved":
                recheck = engine.evaluate(p["tool"], p["args"])
                if not recheck.allowed:
                    p["status"], p["execution_error"] = "blocked_at_execution", "; ".join(recheck.reasons)
                    log("action_blocked", {"proposal_id": p["proposal_id"], "reasons": recheck.reasons}, actor="policy_engine")
                else:
                    try:
                        rec = deps.actions.execute(tenant_id=deps.view.tenant_id, proposal=p, approval=p["approval"])
                        p["status"] = "executed"
                        p["execution"] = {"record_id": rec["id"], "duplicate": bool(rec.get("duplicate")), "at": rec["created_at"]}
                        log("action_executed", {"proposal_id": p["proposal_id"], "tool": p["tool"], "args": p["args"],
                                                "record_id": rec["id"], "approved_by": p["approval"]["approver"]}, actor="executor")
                    except ApprovalMissing as exc:
                        p["status"], p["execution_error"] = "blocked_at_execution", str(exc)
                        log("action_blocked", {"proposal_id": p["proposal_id"], "reasons": [str(exc)]}, actor="executor")
            proposals.append(p)
        return {"proposals": proposals, "evidence": ledger.to_list()}

    def compose(state: AgentState) -> dict:
        guard = state.get("guard") or {}
        if not guard.get("allowed", True):
            return {"answer": f"Request refused ({guard.get('category')}): {guard.get('reason')}"}
        draft = state.get("draft") or ""
        prefix = "".join(f"Note: {w}\n\n" for w in guard.get("warnings", []))
        return {"answer": prefix + draft + action_status_section(state.get("proposals", []))}

    def verify(state: AgentState) -> dict:
        guard = state.get("guard") or {}
        if not guard.get("allowed", True):
            return {"grounding": {"passed": True, "skipped": "refused_by_guard"}, "grounding_status": "passed"}
        ledger = EvidenceLedger(state.get("evidence", []))
        not_found = {v for r in state.get("tool_calls", []) if not r.get("ok") and "not found" in (r.get("error") or "")
                     for v in (r.get("args") or {}).values() if isinstance(v, str)}
        not_found = {v for v in not_found if not deps.store.locate_device(v)}  # never whitelist another tenant's real IDs
        rep = deps.verifier.verify(state.get("answer") or "", ledger, acknowledged_unknown=not_found)
        log("grounding_check", {"turn": state["turn"], "attempt": state.get("revisions", 0) + 1, **rep.to_dict()}, actor="verifier")
        if rep.passed:
            return {"grounding": rep.to_dict(), "grounding_status": "passed"}
        if state.get("revisions", 0) < s.max_revisions:
            res = deps.reasoner.revise(state, rctx(state), rep.issues)
            if res.draft:
                return {"grounding": rep.to_dict(), "grounding_status": "retry", "draft": res.draft,
                        "revisions": state.get("revisions", 0) + 1,
                        "transcript": state.get("transcript", []) + res.transcript_add}
        fallback = evidence_only_answer(state, ledger) + action_status_section(state.get("proposals", []))
        rep2 = deps.verifier.verify(fallback, ledger, acknowledged_unknown=not_found)
        log("grounding_fallback", {"turn": state["turn"], "issues": rep.issues, "fallback_passed": rep2.passed}, actor="verifier")
        return {"answer": fallback, "grounding": {**rep2.to_dict(), "fallback": True, "original_issues": rep.issues},
                "grounding_status": "fallback"}

    def finalize(state: AgentState) -> dict:
        refused = not (state.get("guard") or {}).get("allowed", True)
        status = "refused" if refused else "answered"
        log("answer", {"turn": state["turn"], "status": status, "answer": state.get("answer"),
                       "grounding_status": state.get("grounding_status"),
                       "proposals": [{"id": p["proposal_id"], "status": p["status"]} for p in state.get("proposals", [])]})
        hist = state.get("history", []) + [{"turn": state["turn"], "question": state["question"], "answer": state.get("answer") or ""}]
        return {"status": status, "history": hist[-10:]}

    # ------------------------------------------------------------------ routing
    def after_guard(state: AgentState) -> str:
        return "plan" if (state.get("guard") or {}).get("allowed") else "compose"

    def after_agent(state: AgentState) -> str:
        return "tools" if state.get("pending_tool_calls") else "reflect"

    def after_reflect(state: AgentState) -> str:
        # Back to the agent when reflection asked for more work, or when the loop ended without a draft
        # (bounded: at max_agent_iterations the agent is forced to answer without tools).
        if state.get("draft") is None and state.get("iterations", 0) <= s.max_agent_iterations:
            return "agent"
        return "policy_gate" if state.get("proposals") else "compose"

    def after_policy(state: AgentState) -> str:
        return "human_approval" if any(p["status"] == "pending_approval" for p in state.get("proposals", [])) else "compose"

    def after_verify(state: AgentState) -> str:
        return "compose" if state.get("grounding_status") == "retry" else "finalize"

    g = StateGraph(AgentState)
    for name, fn in [("guard_input", guard_input), ("plan", plan), ("agent", agent), ("tools", tools), ("reflect", reflect),
                     ("policy_gate", policy_gate), ("human_approval", human_approval), ("execute_actions", execute_actions),
                     ("compose", compose), ("verify", verify), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "guard_input")
    g.add_conditional_edges("guard_input", after_guard, ["plan", "compose"])
    g.add_edge("plan", "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "reflect"])
    g.add_edge("tools", "agent")
    g.add_conditional_edges("reflect", after_reflect, ["agent", "policy_gate", "compose"])
    g.add_conditional_edges("policy_gate", after_policy, ["human_approval", "compose"])
    g.add_edge("human_approval", "execute_actions")
    g.add_edge("execute_actions", "compose")
    g.add_edge("compose", "verify")
    g.add_conditional_edges("verify", after_verify, ["compose", "finalize"])
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
