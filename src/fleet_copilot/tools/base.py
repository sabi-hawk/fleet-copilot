"""Tool abstractions shared by the read, insight and action tools.

Design rules for every tool:
- Tenant scope is bound at construction (via `ToolContext.view`); no tool takes
  a company_id parameter, so the model cannot even express a cross-tenant call.
- Arguments are validated with a strict pydantic model (unknown fields are
  rejected) before the handler runs; validation errors go back to the model as
  `is_error` tool results so it can self-correct.
- Every factual output is registered in the evidence ledger and returned with
  its citation IDs; the model sees `{summary, data, evidence: {E#: claim}}`.
- Action tools never change state: they create proposals that the policy gate
  and a human must approve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from fleet_copilot.config import Settings
from fleet_copilot.data.store import NotFoundInTenant, TenantView
from fleet_copilot.evidence import EvidenceLedger


@dataclass
class ToolContext:
    view: TenantView
    ledger: EvidenceLedger
    settings: Settings
    policy: Any  # ActionPolicyEngine (typed loosely to avoid an import cycle)
    session_id: str
    turn: int
    proposals: list[dict[str, Any]] = field(default_factory=list)
    on_security_event: Callable[[str, dict[str, Any]], None] | None = None


@dataclass
class ToolOutput:
    summary: str
    data: Any
    evidence_ids: list[str] = field(default_factory=list)
    proposal: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    kind: str  # "read" | "insight" | "action"
    handler: Callable[[ToolContext, Any], ToolOutput]

    def input_schema(self) -> dict[str, Any]:
        return _strip_titles(self.args_model.model_json_schema())

    def anthropic_tool(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema()}


def _strip_titles(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]):
        self._specs = {s.name: s for s in specs}

    @property
    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self, kind: str | None = None) -> list[str]:
        return [s.name for s in self._specs.values() if kind is None or s.kind == kind]

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [s.anthropic_tool() for s in self._specs.values()]

    def execute(self, call_id: str, name: str, raw_args: dict[str, Any] | None, ctx: ToolContext) -> dict[str, Any]:
        record: dict[str, Any] = {"id": call_id, "name": name, "args": raw_args or {}, "ok": False,
                                  "summary": "", "evidence_ids": [], "proposal_id": None, "error": None}
        spec = self._specs.get(name)
        if spec is None:
            record["error"] = f"Unknown tool '{name}'. Available tools: {', '.join(self._specs)}"
        else:
            record["kind"] = spec.kind
            try:
                args = spec.args_model.model_validate(raw_args or {})
                out = spec.handler(ctx, args)
                record.update(ok=True, summary=out.summary, evidence_ids=out.evidence_ids, data=out.data)
                if out.proposal:
                    record["proposal_id"] = out.proposal["proposal_id"]
            except ValidationError as exc:
                errs = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'args'}: {e['msg']}" for e in exc.errors())
                record["error"] = f"Invalid arguments for {name}: {errs}"
            except NotFoundInTenant as exc:
                record["error"] = str(exc)
                if ctx.on_security_event:
                    ctx.on_security_event("tenant_lookup_denied", {"tool": name, "kind": exc.kind, "identifier": exc.identifier})
            except ValueError as exc:
                record["error"] = f"{name} failed: {exc}"
        record["content"] = render_for_model(record, ctx.ledger)
        return record


def render_for_model(record: dict[str, Any], ledger: EvidenceLedger) -> str:
    if not record["ok"]:
        return json.dumps({"error": record["error"]})
    payload = {
        "summary": record["summary"],
        "data": record.get("data"),
        "evidence": ledger.claims(record["evidence_ids"]),
    }
    return json.dumps(payload, default=str, ensure_ascii=False)
