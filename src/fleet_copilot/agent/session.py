"""CopilotSession: the public entry point used by the CLI, the eval runner and tests.

A session is bound to exactly one tenant at creation (in a real deployment the
tenant comes from the administrator's authenticated identity, never from the
prompt). Each session is one checkpointed LangGraph thread; `ask` runs a turn
and `resolve_approval` resumes a turn paused at the human-approval interrupt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fleet_copilot.actions import ActionStore
from fleet_copilot.agent.graph import AgentDeps, build_graph
from fleet_copilot.agent.reasoners import ClaudeReasoner, RuleBasedReasoner
from fleet_copilot.agent.state import TURN_RESET
from fleet_copilot.audit import AuditLog
from fleet_copilot.config import Settings, llm_credentials_available
from fleet_copilot.data.store import TelemetryStore
from fleet_copilot.evidence import EvidenceLedger
from fleet_copilot.guardrails import GroundingVerifier, InputGuard
from fleet_copilot.tools import build_registry


@dataclass
class TurnResult:
    status: str  # answered | refused | awaiting_approval
    answer: str | None
    question: str
    turn: int
    plan: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    approval_request: dict[str, Any] | None = None
    grounding: dict[str, Any] | None = None
    grounding_status: str | None = None
    guard: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def cited_evidence(self) -> list[dict[str, Any]]:
        from fleet_copilot.guardrails import extract_citations

        ids = set(extract_citations(self.answer or ""))
        return [e for e in self.evidence if e["id"] in ids]


def make_reasoner(kind: str, settings: Settings, client: Any | None = None):
    if kind == "auto":
        kind = "claude" if (client is not None or llm_credentials_available()) else "rule"
    if kind in ("claude", "llm"):
        return ClaudeReasoner(settings, client=client)
    if kind in ("rule", "rule_based", "offline"):
        return RuleBasedReasoner()
    raise ValueError(f"Unknown reasoner '{kind}' (use auto, claude or rule)")


class CopilotSession:
    def __init__(
        self,
        store: TelemetryStore,
        tenant_id: str,
        settings: Settings | None = None,
        reasoner: Any | None = None,
        audit: AuditLog | None = None,
        actions: ActionStore | None = None,
        session_id: str | None = None,
        actor: str = "it-admin",
    ):
        self.settings = settings or Settings.from_env()
        self.store = store
        self.tenant_id = tenant_id
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        self.audit = audit or AuditLog(self.settings.audit_path)
        self.actions = actions or ActionStore(self.settings.actions_path)
        self.reasoner = reasoner or make_reasoner("auto", self.settings)

        def on_security_event(kind: str, data: dict[str, Any]) -> None:
            self.audit.record("security_event", tenant_id=tenant_id, session_id=self.session_id, actor="tenant_guard",
                              data={"type": kind, **data})

        def on_cross_tenant(kind: str, identifier: str, owner: str) -> None:
            on_security_event("cross_tenant_lookup", {"kind": kind, "identifier": identifier, "owner_tenant": owner})

        self.view = store.tenant(tenant_id, on_cross_tenant=on_cross_tenant)
        deps = AgentDeps(
            store=store, view=self.view, registry=build_registry(), reasoner=self.reasoner, settings=self.settings,
            audit=self.audit, actions=self.actions, guard=InputGuard(store, tenant_id),
            verifier=GroundingVerifier(store, tenant_id), session_id=self.session_id, on_security_event=on_security_event,
        )
        self.graph = build_graph(deps, InMemorySaver())
        self._config = {"configurable": {"thread_id": self.session_id}, "recursion_limit": 100}
        self.turn = 0
        self.audit.record("session_start", tenant_id=tenant_id, session_id=self.session_id, actor=actor,
                          data={"reasoner": self.reasoner.name, "model": self.settings.model if self.reasoner.name == "claude" else None,
                                "dataset": str(self.settings.dataset_path)})

    # ------------------------------------------------------------------ public API
    @property
    def pending_approval(self) -> dict[str, Any] | None:
        snap = self.graph.get_state(self._config)
        for intr in getattr(snap, "interrupts", ()) or ():
            return intr.value
        return None

    def ask(self, question: str) -> TurnResult:
        if self.pending_approval is not None:
            # A new question supersedes an unresolved approval request: fail closed (reject everything).
            self.resolve_approval({}, approver=None)
        self.turn += 1
        payload: dict[str, Any] = {**TURN_RESET, "question": question, "turn": self.turn}
        if self.turn == 1:
            payload.update(session_id=self.session_id, tenant_id=self.tenant_id, transcript=[], evidence=[], history=[])
        self.graph.invoke(payload, self._config)
        return self._result()

    def resolve_approval(self, decisions: dict[str, str], approver: str | None) -> TurnResult:
        if self.pending_approval is None:
            raise RuntimeError("No approval request is pending in this session.")
        self.graph.invoke(Command(resume={"approver": approver, "decisions": decisions}), self._config)
        return self._result()

    def approve_all(self, approver: str) -> TurnResult:
        req = self.pending_approval or {}
        return self.resolve_approval({p["proposal_id"]: "approve" for p in req.get("proposals", [])}, approver)

    def reject_all(self, approver: str) -> TurnResult:
        req = self.pending_approval or {}
        return self.resolve_approval({p["proposal_id"]: "reject" for p in req.get("proposals", [])}, approver)

    @property
    def ledger(self) -> EvidenceLedger:
        return EvidenceLedger(self.graph.get_state(self._config).values.get("evidence", []))

    # ------------------------------------------------------------------ helpers
    def _result(self) -> TurnResult:
        snap = self.graph.get_state(self._config)
        v = snap.values
        req = None
        for intr in getattr(snap, "interrupts", ()) or ():
            req = intr.value
        status = "awaiting_approval" if req is not None else v.get("status", "answered")
        return TurnResult(
            status=status,
            answer=v.get("answer") if req is None else None,
            question=v.get("question", ""),
            turn=v.get("turn", self.turn),
            plan=v.get("plan") or {},
            tool_calls=[{k: r.get(k) for k in ("name", "args", "ok", "error", "summary", "evidence_ids", "proposal_id")}
                        for r in v.get("tool_calls", [])],
            proposals=v.get("proposals", []),
            approval_request=req,
            grounding=v.get("grounding"),
            grounding_status=v.get("grounding_status"),
            guard=v.get("guard") or {},
            evidence=v.get("evidence", []),
        )
