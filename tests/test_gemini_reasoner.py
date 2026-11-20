"""Gemini reasoner loop tests against a scripted fake client."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from google.genai import errors, types

from fleet_copilot.agent.reasoners.gemini import GeminiReasoner, QuotaExhausted


def model_resp(*parts, finish="STOP"):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=list(parts)), finish_reason=finish)])


def call(name, args, sig=None):
    return types.Part(function_call=types.FunctionCall(name=name, args=args), thought_signature=sig)


def txt(t):
    return types.Part(text=t)


def last_payloads(contents):
    for c in reversed(contents):
        frs = [p.function_response for p in (c.parts or []) if p.function_response]
        if frs:
            return [fr.response for fr in frs]
    return []


class FakeGemini:
    def __init__(self, plan_steps, script):
        self.requests: list[dict] = []
        self.plan = {"intent": "question", "goal": "g", "steps": plan_steps, "needs_actions": False, "assumptions": []}
        self.script = list(script)
        self.models = NS(generate_content=self._gen)

    def _gen(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        if config.response_json_schema:
            return model_resp(txt(json.dumps(self.plan)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(contents) if callable(step) else step

    def agent_requests(self):
        return [r for r in self.requests if not r["config"].response_json_schema]


def low_disk_answer(contents):
    p = last_payloads(contents)[0]
    rows = p["data"]["devices"]
    body = "; ".join(f"{r['device_id']} has {r['disk_free_pct']}% free [{r['evidence_id']}]" for r in rows)
    return model_resp(txt(f"{len(rows)} devices are low on disk [{p['data']['scope_evidence_id']}]: {body}."))


def session(make_session, settings, fake, tenant="acme-001", **overrides):
    s = replace(settings, **overrides) if overrides else settings
    return make_session(tenant, reasoner=GeminiReasoner(s, client=fake), settings_override=s)


def test_tool_loop_config_and_transcript(make_session, settings):
    fake = FakeGemini([{"description": "find low-disk devices", "tools": ["find_devices"]}],
                      [model_resp(call("find_devices", {"low_disk": True}, sig=b"\x01sig\xff")), low_disk_answer])
    s = session(make_session, settings, fake)
    r = s.ask("Which devices are low on disk space?")
    assert r.status == "answered" and r.grounding_status == "passed", r.grounding
    assert "8NM23J95R5I6" in r.answer and "MT7PJB7N5LRE" in r.answer
    for req in fake.requests:
        cfg = req["config"]
        assert req["model"] == settings.gemini_model
        assert cfg.temperature == 0.0 and cfg.seed == 7 and cfg.automatic_function_calling.disable is True
        assert cfg.thinking_config.thinking_level == "LOW"
    first, second = fake.agent_requests()
    assert first["config"].tool_config.function_calling_config.mode == "AUTO"
    assert len(first["config"].tools[0].function_declarations) == 18
    roles = [c.role for c in second["contents"]]
    assert roles == ["user", "model", "user"]
    assert second["contents"][1].parts[0].thought_signature == b"\x01sig\xff"   # replayed unchanged
    fr = second["contents"][2].parts[0].function_response
    assert fr.name == "find_devices" and "evidence" in fr.response


def test_cross_tenant_refused_before_any_call(make_session, settings):
    fake = FakeGemini([], [])
    r = session(make_session, settings, fake).ask("List Initech laptops with low disk")
    assert r.status == "refused" and fake.requests == []


def test_revision_uses_function_calling_none(make_session, settings):
    fake = FakeGemini([{"description": "find", "tools": ["find_devices"]}],
                      [model_resp(call("find_devices", {"low_disk": True})),
                       model_resp(txt("Q3VUITEGQ400 is low on disk [E77].")),
                       low_disk_answer])
    r = session(make_session, settings, fake).ask("Which devices are low on disk space?")
    assert r.grounding_status == "passed" and "Q3VUITEGQ400" not in r.answer
    assert fake.agent_requests()[2]["config"].tool_config.function_calling_config.mode == "NONE"


def test_blocked_response_is_handled(make_session, settings):
    fake = FakeGemini([], [model_resp(txt(""), finish="SAFETY")])
    r = session(make_session, settings, fake).ask("Summarise the fleet")
    assert r.status == "answered" and r.answer.startswith("I can't help with that request")


def test_rate_limit_retry_then_success(make_session, settings, monkeypatch):
    monkeypatch.setattr("fleet_copilot.agent.reasoners.gemini.time.sleep", lambda s: None)
    rate = errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED",
                                              "details": [{"retryDelay": "3s"}]}})
    fake = FakeGemini([], [rate, model_resp(txt("No telemetry facts to report."))])
    r = session(make_session, settings, fake).ask("Summarise the fleet")
    assert r.status == "answered" and len(fake.agent_requests()) == 2


def test_daily_quota_exhaustion_stops_clearly(make_session, settings, monkeypatch):
    monkeypatch.setattr("fleet_copilot.agent.reasoners.gemini.time.sleep", lambda s: None)
    daily = errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                               "message": "Quota exceeded for metric generate_content_free_tier_requests, limit PerDay"}})
    fake = FakeGemini([], [daily])
    with pytest.raises(QuotaExhausted):
        session(make_session, settings, fake).ask("Summarise the fleet")


def test_gemini_proposals_go_through_policy_and_approval(make_session, settings):
    fake = FakeGemini([{"description": "evidence", "tools": ["detect_memory_pressure"]}],
                      [model_resp(call("detect_memory_pressure", {})),
                       model_resp(call("create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "memory", "spec": "32 GB"}),
                                  call("create_upgrade_order", {"device_id": "1LYSSFD074BB", "component": "memory", "spec": "64 GB"})),
                       model_resp(txt("Proposed a 32 GB upgrade for M4XVHUV1MEPZ (pending approval) [E1]; the 1LYSSFD074BB order was rejected."))])
    s = session(make_session, settings, fake)
    r = s.ask("Order RAM upgrades where needed")
    assert r.status == "awaiting_approval"
    assert [p["args"]["device_id"] for p in r.approval_request["proposals"]] == ["M4XVHUV1MEPZ"]
    r = s.approve_all("alice@acme")
    assert {p["args"]["device_id"]: p["status"] for p in r.proposals} == {"M4XVHUV1MEPZ": "executed", "1LYSSFD074BB": "rejected_by_policy"}
