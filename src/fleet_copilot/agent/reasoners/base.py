"""Reasoner interface used by the agent graph."""

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
