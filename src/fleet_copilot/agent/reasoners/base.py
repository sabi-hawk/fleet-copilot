"""Reasoner interface: the swappable 'brain' of the agent graph.

The graph (guard -> plan -> agent<->tools loop -> reflect -> policy -> approval
-> execute -> compose -> verify) is identical for every reasoner. A reasoner
only decides (a) the plan, (b) the next tool calls or the draft answer, and
(c) how to revise a draft the grounding verifier rejected.

Two implementations ship:
- ClaudeReasoner: Claude via the Anthropic SDK (planning with structured
  output, native tool calling, revision on verifier feedback).
- RuleBasedReasoner: deterministic intent parser + templated composer. Used
  for keyless runs, CI and as the reproducible baseline in evaluations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from fleet_copilot.config import Settings
from fleet_copilot.data.store import TenantView
from fleet_copilot.evidence import EvidenceLedger
from fleet_copilot.tools.base import ToolRegistry


@dataclass
class ReasonerContext:
    registry: ToolRegistry
    view: TenantView
    settings: Settings
    ledger: EvidenceLedger


@dataclass
class PlanResult:
    plan: dict[str, Any]
    transcript_add: list[dict[str, Any]] = field(default_factory=list)
    reasoner_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActResult:
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    draft: str | None = None
    transcript_add: list[dict[str, Any]] = field(default_factory=list)
    reasoner_state: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None


class Reasoner(Protocol):
    name: str

    def plan(self, state: dict[str, Any], rctx: ReasonerContext) -> PlanResult: ...

    def act(self, state: dict[str, Any], rctx: ReasonerContext, force_final: bool = False, note: str | None = None) -> ActResult: ...

    def observe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def revise(self, state: dict[str, Any], rctx: ReasonerContext, issues: list[str]) -> ActResult: ...
