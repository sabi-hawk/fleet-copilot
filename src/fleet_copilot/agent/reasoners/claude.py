"""Claude-backed reasoner (Anthropic Python SDK, called from LangGraph nodes).

- plan(): one structured-output call (JSON schema) producing a tool-level plan.
- act(): one Messages API call with the tenant-scoped tools; returns tool_use
  calls for the graph's tool node, or the final draft answer.
- revise(): re-asks for an answer (tools disabled) with the grounding
  verifier's issues when the draft cited unknown evidence or unsupported IDs.

Model behaviour notes (claude-opus-5-5 by default, configurable):
- No sampling parameters are sent (current models reject temperature); answer
  determinism comes from deterministic tools, evidence IDs and verification.
- Thinking is adaptive and always on for current models; effort is set via
  output_config (FLEET_COPILOT_EFFORT, default "high").
- The transcript is append-only and assistant content blocks (including
  thinking blocks) are passed back unchanged.
- Server-side refusal fallback is enabled by default on models that support
  it (FLEET_COPILOT_REFUSAL_FALLBACK=off to disable).
"""

from __future__ import annotations

import json
from typing import Any

from fleet_copilot.agent.prompts import (
    AGENT_SYSTEM,
    PLANNER_SYSTEM,
    agent_turn_message,
    budget_message,
    revision_message,
    turn_context,
)
from fleet_copilot.agent.reasoners.base import ActResult, PlanResult, ReasonerContext
from fleet_copilot.config import REFUSAL_FALLBACK_MODELS, Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["question", "insight", "action", "out_of_scope", "clarification"]},
        "goal": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}, "tools": {"type": "array", "items": {"type": "string"}}},
                "required": ["description", "tools"],
                "additionalProperties": False,
            },
        },
        "needs_actions": {"type": "boolean"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "goal", "steps", "needs_actions", "assumptions"],
    "additionalProperties": False,
}


def _get(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def block_to_param(block: Any) -> dict[str, Any]:
    """Convert an SDK response content block into a request-ready dict, unchanged in substance."""
    t = _get(block, "type")
    if t == "text":
        return {"type": "text", "text": _get(block, "text") or ""}
    if t == "tool_use":
        return {"type": "tool_use", "id": _get(block, "id"), "name": _get(block, "name"), "input": _get(block, "input") or {}}
    if t == "thinking":
        return {"type": "thinking", "thinking": _get(block, "thinking") or "", "signature": _get(block, "signature")}
    if t == "redacted_thinking":
        return {"type": "redacted_thinking", "data": _get(block, "data")}
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return dict(block)


class ClaudeReasoner:
    name = "claude"

    def __init__(self, settings: Settings, client: Any | None = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.settings = settings
        self.use_fallback = settings.refusal_fallback and settings.model in REFUSAL_FALLBACK_MODELS

    # ------------------------------------------------------------------ API plumbing
    def _create(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                tool_choice: dict | None = None, output_format: dict | None = None) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        output_config: dict[str, Any] = {}
        if self.settings.effort and self.settings.effort.lower() not in ("none", "default"):
            output_config["effort"] = self.settings.effort
        if output_format:
            output_config["format"] = output_format
        if output_config:
            kwargs["output_config"] = output_config
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if self.use_fallback:
            return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    @staticmethod
    def _text(content: list[dict]) -> str:
        return "\n".join(b["text"] for b in content if b.get("type") == "text").strip()

    # ------------------------------------------------------------------ planning
    def plan(self, state: dict[str, Any], rctx: ReasonerContext) -> PlanResult:
        v = rctx.view
        ctxline = turn_context(v.display_name, v.tenant_id, v.window_label, v.as_of, len(v.device_ids()))
        catalog = "\n".join(f"- {s.name} [{s.kind}]: {s.description}" for s in rctx.registry.specs)
        recent = state.get("history", [])[-3:]
        hist = "\n".join(f"- Q: {h['question']}\n  A: {h['answer'][:400]}" for h in recent)
        msg = (f"{ctxline}\n\nTool catalog:\n{catalog}\n\n"
               + (f"Recent conversation (for follow-up references):\n{hist}\n\n" if hist else "")
               + f"Request: {state['question']}\n\nReturn the plan as JSON.")
        resp = self._create(system=PLANNER_SYSTEM, messages=[{"role": "user", "content": msg}],
                            output_format={"type": "json_schema", "schema": PLAN_SCHEMA})
        known = set(rctx.registry.names())
        if _get(resp, "stop_reason") == "refusal":
            plan = {"intent": "out_of_scope", "goal": state["question"], "steps": [], "needs_actions": False,
                    "assumptions": [], "refused": True}
        else:
            content = [block_to_param(b) for b in _get(resp, "content") or []]
            try:
                plan = json.loads(self._text(content))
            except (json.JSONDecodeError, TypeError):
                plan = {"intent": "question", "goal": state["question"], "steps": [], "needs_actions": False,
                        "assumptions": ["planner output could not be parsed; proceeding without a fixed plan"]}
        for step in plan.get("steps", []):
            step["tools"] = [t for t in step.get("tools", []) if t in known]
        plan["reasoner"] = self.name
        opening = {"role": "user", "content": agent_turn_message(ctxline, state["question"], plan)}
        return PlanResult(plan=plan, transcript_add=[opening])

    # ------------------------------------------------------------------ acting
    def _call_agent(self, state: dict[str, Any], rctx: ReasonerContext, add: list[dict], tool_choice: dict | None) -> ActResult:
        messages = list(state.get("transcript", [])) + add
        resp = self._create(system=AGENT_SYSTEM, messages=messages, tools=rctx.registry.anthropic_tools(), tool_choice=tool_choice)
        content = [block_to_param(b) for b in _get(resp, "content") or []]
        add = add + [{"role": "assistant", "content": content}]
        stop = _get(resp, "stop_reason")
        if stop == "refusal":
            return ActResult(draft="I can't help with that request.", transcript_add=add, stop_reason=stop)
        calls = [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}} for b in content if b.get("type") == "tool_use"]
        if calls and stop == "tool_use" and tool_choice is None:
            return ActResult(tool_calls=calls, transcript_add=add, stop_reason=stop)
        draft = self._text(content)
        if stop == "max_tokens":
            draft += "\n\n(Answer truncated: output limit reached.)"
        return ActResult(draft=draft, transcript_add=add, stop_reason=stop)

    def act(self, state: dict[str, Any], rctx: ReasonerContext, force_final: bool = False, note: str | None = None) -> ActResult:
        add: list[dict] = []
        if note:
            add.append({"role": "user", "content": note})
        if force_final:
            add.append({"role": "user", "content": budget_message()})
            return self._call_agent(state, rctx, add, {"type": "none"})
        return self._call_agent(state, rctx, add, None)

    def observe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not records:
            return []
        blocks = []
        for r in records:
            block = {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
            if not r.get("ok"):
                block["is_error"] = True
            blocks.append(block)
        return [{"role": "user", "content": blocks}]

    def revise(self, state: dict[str, Any], rctx: ReasonerContext, issues: list[str]) -> ActResult:
        return self._call_agent(state, rctx, [{"role": "user", "content": revision_message(issues)}], {"type": "none"})
