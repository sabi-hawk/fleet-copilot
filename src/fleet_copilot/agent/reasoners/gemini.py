"""Gemini reasoner (google-genai SDK)."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from fleet_copilot.agent.prompts import (
    AGENT_SYSTEM,
    PLAN_SCHEMA,
    PLANNER_SYSTEM,
    agent_turn_message,
    budget_message,
    normalize_plan,
    planner_message,
    revision_message,
)
from fleet_copilot.agent.reasoners.base import ActResult, PlanResult, ReasonerContext
from fleet_copilot.config import Settings

MAX_OUTPUT_TOKENS = 8192
SEED = 7
RETRYABLE = {429, 500, 502, 503, 504}
BLOCKED_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}
_GEN_ID = "gm-"


class QuotaExhausted(RuntimeError):
    pass


def _retry_delay(exc: Any, attempt: int) -> float:
    text = json.dumps(getattr(exc, "details", None) or {}, default=str) + str(exc)
    m = re.search(r'retryDelay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)s', text)
    return min(float(m.group(1)) + 1.0, 90.0) if m else min(2.0 * (2 ** attempt), 60.0)


class GeminiReasoner:
    name = "gemini"

    def __init__(self, settings: Settings, client: Any | None = None):
        from google.genai import types

        self.types = types
        if client is None:
            from google import genai

            key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(settings.llm_timeout_s * 1000)))
        self.client = client
        self.settings = settings
        self.model = settings.gemini_model
        self._last_call = 0.0
        self._tools_cache: list | None = None
    def _tools(self, rctx: ReasonerContext) -> list:
        if self._tools_cache is None:
            t = self.types
            decls = [t.FunctionDeclaration(name=s.name, description=s.description, parameters_json_schema=s.input_schema())
                     for s in rctx.registry.specs]
            self._tools_cache = [t.Tool(function_declarations=decls)]
        return self._tools_cache

    def _generate(self, contents: list, *, system: str, tools: list | None = None, mode: str | None = None,
                  json_schema: dict | None = None) -> Any:
        t = self.types
        cfg: dict[str, Any] = {
            "system_instruction": system,
            "temperature": 0.0,
            "seed": SEED,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "automatic_function_calling": t.AutomaticFunctionCallingConfig(disable=True),
        }
        if tools:
            cfg["tools"] = tools
            cfg["tool_config"] = t.ToolConfig(function_calling_config=t.FunctionCallingConfig(mode=mode or "AUTO"))
        if json_schema:
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = json_schema
        level = (self.settings.gemini_thinking_level or "").upper()
        if level and level not in ("NONE", "DEFAULT"):
            cfg["thinking_config"] = t.ThinkingConfig(thinking_level=level)
        config = t.GenerateContentConfig(**cfg)
        from google.genai import errors

        attempt = 0
        while True:
            wait = self.settings.llm_min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                self._last_call = time.monotonic()
                return self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except errors.APIError as exc:
                msg = str(exc)
                if exc.code == 429 and re.search(r"per ?day|PerDay|daily", msg + json.dumps(getattr(exc, "details", None), default=str), re.IGNORECASE):
                    info = re.findall(r'"quota(?:Id|Value)": "([^"]+)"', json.dumps(getattr(exc, "details", None), default=str))
                    raise QuotaExhausted(f"Gemini daily quota exhausted for {self.model} ({' / '.join(info) or 'see AI Studio rate limits'})") from exc
                if exc.code not in RETRYABLE or attempt >= 5:
                    raise
                time.sleep(_retry_delay(exc, attempt))
                attempt += 1
            except Exception as exc:  # network timeouts (httpx / httpx2) are retried a few times
                if "timeout" not in type(exc).__name__.lower() or attempt >= 2:
                    raise
                time.sleep(_retry_delay(exc, attempt))
                attempt += 1

    @staticmethod
    def _dump(content: Any) -> dict[str, Any]:
        return content.model_dump(mode="json", exclude_none=True) if hasattr(content, "model_dump") else dict(content)

    def _contents(self, transcript: list[dict]) -> list:
        return [self.types.Content.model_validate(c) for c in transcript]

    def _user(self, text: str) -> dict[str, Any]:
        return {"role": "user", "parts": [{"text": text}]}
    def plan(self, state: dict[str, Any], rctx: ReasonerContext) -> PlanResult:
        ctxline, msg = planner_message(state, rctx)
        resp = self._generate([self.types.Content.model_validate(self._user(msg))], system=PLANNER_SYSTEM, json_schema=PLAN_SCHEMA)
        try:
            plan = json.loads(resp.text or "")
            if not isinstance(plan, dict):
                raise ValueError("plan is not an object")
        except (json.JSONDecodeError, ValueError, TypeError):
            plan = {"intent": "question", "goal": state["question"], "steps": [], "needs_actions": False,
                    "assumptions": ["planner output could not be parsed; proceeding without a fixed plan"]}
        plan = normalize_plan(plan, state["question"], set(rctx.registry.names()), self.name)
        return PlanResult(plan=plan, transcript_add=[self._user(agent_turn_message(ctxline, state["question"], plan))])
    def _call_agent(self, state: dict[str, Any], rctx: ReasonerContext, add: list[dict], mode: str) -> ActResult:
        transcript = list(state.get("transcript", [])) + add
        resp = self._generate(self._contents(transcript), system=AGENT_SYSTEM, tools=self._tools(rctx), mode=mode)
        cands = getattr(resp, "candidates", None) or []
        if not cands or getattr(cands[0], "content", None) is None:
            finish = str(getattr(cands[0], "finish_reason", "") if cands else "BLOCKED")
            return ActResult(draft="I can't help with that request.", transcript_add=add, stop_reason=f"blocked:{finish}")
        cand = cands[0]
        finish = getattr(getattr(cand, "finish_reason", None), "value", None) or str(getattr(cand, "finish_reason", ""))
        content = self._dump(cand.content)
        content["role"] = "model"
        add = add + [content]
        if finish in BLOCKED_FINISH:
            return ActResult(draft="I can't help with that request.", transcript_add=add, stop_reason=f"blocked:{finish}")
        if finish == "MALFORMED_FUNCTION_CALL":
            return ActResult(transcript_add=add, stop_reason=finish, reasoner_state={
                "pending_note": "Your previous function call was malformed. Call the tool again with valid JSON arguments that match its schema."})
        parts = content.get("parts") or []
        turn, n = state.get("turn", 0), len(state.get("tool_calls", []))
        calls = []
        for i, p in enumerate(parts):
            fc = p.get("function_call")
            if fc:
                calls.append({"id": fc.get("id") or f"{_GEN_ID}{turn}-{n + i}", "name": fc.get("name"), "args": fc.get("args") or {}})
        if calls and mode != "NONE":
            return ActResult(tool_calls=calls, transcript_add=add, stop_reason="tool_use")
        draft = "\n".join(p["text"] for p in parts if p.get("text") and not p.get("thought")).strip()
        if finish == "MAX_TOKENS":
            draft += "\n\n(Answer truncated: output limit reached.)"
        return ActResult(draft=draft, transcript_add=add, stop_reason=finish or "STOP")

    def act(self, state: dict[str, Any], rctx: ReasonerContext, force_final: bool = False, note: str | None = None) -> ActResult:
        add: list[dict] = []
        if note:
            add.append(self._user(note))
        if force_final:
            add.append(self._user(budget_message()))
            return self._call_agent(state, rctx, add, "NONE")
        return self._call_agent(state, rctx, add, "AUTO")

    def observe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not records:
            return []
        parts = []
        for r in records:
            payload = json.loads(r["content"])
            fr: dict[str, Any] = {"name": r["name"], "response": payload if r.get("ok") else {"error": payload.get("error")}}
            if not str(r["id"]).startswith(_GEN_ID):
                fr["id"] = r["id"]
            parts.append({"function_response": fr})
        return [{"role": "user", "parts": parts}]

    def revise(self, state: dict[str, Any], rctx: ReasonerContext, issues: list[str]) -> ActResult:
        return self._call_agent(state, rctx, [self._user(revision_message(issues))], "NONE")
