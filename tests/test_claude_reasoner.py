"""ClaudeReasoner wiring tests against a scripted fake Anthropic client.

These verify the agent loop mechanics without network access: request shape
(model, effort, refusal fallback, no sampling params), tool_use -> tool_result
round trips, append-only transcripts with thinking blocks passed back
unchanged, revision on grounding failure, reflection, budget enforcement, and
that the input guard stops cross-tenant requests before any API call.
They do NOT measure answer quality of the real model (see README).
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace as NS

from fleet_copilot.agent.reasoners import ClaudeReasoner
from fleet_copilot.agent.reasoners.claude import FALLBACK_BETA


def text(t):
    return NS(type="text", text=t)


def tool_use(i, name, args):
    return NS(type="tool_use", id=i, name=name, input=args)


def thinking():
    return NS(type="thinking", thinking="", signature="sig-abc")


def resp(content, stop="end_turn"):
    return NS(content=content, stop_reason=stop)


def payloads(kw):
    """Tool payloads from the most recent tool_result message."""
    for m in reversed(kw["messages"]):
        if m["role"] == "user" and isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"]):
            return [json.loads(b["content"]) for b in m["content"]]
    return []


class FakeClaude:
    def __init__(self, plan_steps, script):
        self.requests: list[dict] = []
        self.plan = {"intent": "question", "goal": "g", "steps": plan_steps, "needs_actions": False, "assumptions": []}
        self.script = list(script)
        self.messages = NS(create=lambda **kw: self._handle("messages", kw))
        self.beta = NS(messages=NS(create=lambda **kw: self._handle("beta", kw)))

    def _handle(self, endpoint, kw):
        self.requests.append({"endpoint": endpoint, **kw})
        if "format" in (kw.get("output_config") or {}):
            return resp([text(json.dumps(self.plan))])
        step = self.script.pop(0)
        return step(kw) if callable(step) else step

    def agent_requests(self):
        return [r for r in self.requests if "format" not in (r.get("output_config") or {})]


def low_disk_answer(kw):
    p = payloads(kw)[0]
    rows = p["data"]["devices"]
    body = "; ".join(f"{r['device_id']} has {r['disk_free_pct']}% free [{r['evidence_id']}]" for r in rows)
    return resp([text(f"{len(rows)} devices are low on disk [{p['data']['scope_evidence_id']}]: {body}.")])


def session(make_session, settings, fake, tenant="acme-001", **overrides):
    s = replace(settings, **overrides) if overrides else settings
    return make_session(tenant, reasoner=ClaudeReasoner(s, client=fake), settings_override=s)


def test_tool_loop_request_shape_and_transcript(make_session, settings):
    fake = FakeClaude([{"description": "find low-disk devices", "tools": ["find_devices"]}],
                      [resp([thinking(), text("Checking."), tool_use("tu1", "find_devices", {"low_disk": True})], "tool_use"),
                       low_disk_answer])
    s = session(make_session, settings, fake)
    r = s.ask("Which devices are low on disk space?")
    assert r.status == "answered" and r.grounding_status == "passed", r.grounding
    assert "8NM23J95R5I6" in r.answer and "MT7PJB7N5LRE" in r.answer
    assert [t["name"] for t in r.tool_calls] == ["find_devices"]
    for req in fake.requests:
        assert req["model"] == "claude-opus-5-5"
        assert req["endpoint"] == "beta" and req["betas"] == [FALLBACK_BETA] and req["fallbacks"] == "default"
        assert req["output_config"]["effort"] == "high"
        assert "temperature" not in req and "top_p" not in req and "thinking" not in req
    second = fake.agent_requests()[1]
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert second["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}  # passed back unchanged
    tr = second["messages"][2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "tu1"
    assert {t["name"] for t in second["tools"]} >= {"find_devices", "create_upgrade_order", "notify_employee"}


def test_no_fallback_param_for_models_without_support(make_session, settings):
    fake = FakeClaude([], [resp([text("Nothing to report.")])])
    s = session(make_session, settings, fake, model="claude-haiku-5-5", effort="none")
    s.ask("Give me an overview")
    for r in fake.requests:
        assert r["endpoint"] == "messages" and "betas" not in r and "fallbacks" not in r
        assert "effort" not in (r.get("output_config") or {})


def test_cross_tenant_is_refused_before_any_model_call(make_session, settings):
    fake = FakeClaude([], [])
    s = session(make_session, settings, fake)
    r = s.ask("Which Globex devices have low disk?")
    assert r.status == "refused" and fake.requests == []


def test_revision_then_pass(make_session, settings):
    fake = FakeClaude([{"description": "find", "tools": ["find_devices"]}],
                      [resp([tool_use("tu1", "find_devices", {"low_disk": True})], "tool_use"),
                       resp([text("8NM23J95R5I6 and Q3VUITEGQ400 are low on disk [E99].")]),  # bad citation + unsupported device
                       low_disk_answer])
    s = session(make_session, settings, fake)
    r = s.ask("Which devices are low on disk space?")
    assert r.grounding_status == "passed"
    revision = fake.agent_requests()[2]
    assert revision["tool_choice"] == {"type": "none"}
    assert "grounding verifier rejected" in revision["messages"][-1]["content"]
    assert "Q3VUITEGQ400" not in r.answer


def test_persistent_grounding_failure_falls_back_to_evidence_only(make_session, settings):
    fake = FakeClaude([{"description": "find", "tools": ["find_devices"]}],
                      [resp([tool_use("tu1", "find_devices", {"low_disk": True})], "tool_use"),
                       resp([text("ABCDEF123456 is low [E1].")]),
                       resp([text("ABCDEF123456 is still low [E1].")])])
    s = session(make_session, settings, fake)
    r = s.ask("Which devices are low on disk space?")
    assert r.grounding_status == "fallback"
    assert r.answer.startswith("I could not produce a narrative answer") and "ABCDEF123456" not in r.answer


def test_reflection_sends_agent_back_once(make_session, settings):
    fake = FakeClaude([{"description": "find", "tools": ["find_devices"]},
                       {"description": "check compliance", "tools": ["get_compliance_status"]}],
                      [resp([tool_use("tu1", "find_devices", {"low_disk": True})], "tool_use"),
                       low_disk_answer,
                       resp([tool_use("tu2", "get_compliance_status", {"check_id": "screen_lock"})], "tool_use"),
                       lambda kw: resp([text(f"Done [{payloads(kw)[0]['data']['devices'][0]['evidence_id']}] (MT7PJB7N5LRE fails screen_lock).")])])
    s = session(make_session, settings, fake)
    r = s.ask("Low disk devices and their screen lock status?")
    assert any("Reflection check" in str(m.get("content")) for req in fake.agent_requests() for m in req["messages"][-1:])
    assert [t["name"] for t in r.tool_calls] == ["find_devices", "get_compliance_status"]
    assert r.grounding_status == "passed"


def test_iteration_budget_forces_final_answer(make_session, settings):
    loop = [resp([tool_use(f"tu{i}", "get_fleet_overview", {})], "tool_use") for i in range(2)]
    fake = FakeClaude([], loop + [resp([text("The fleet overview was retrieved [E1].")])])
    s = session(make_session, settings, fake, max_agent_iterations=2)
    r = s.ask("Give me an overview of the fleet")
    final = fake.agent_requests()[-1]
    assert final["tool_choice"] == {"type": "none"} and "budget" in final["messages"][-1]["content"]
    assert r.status == "answered"


def test_llm_proposed_actions_go_through_policy_and_approval(make_session, settings):
    fake = FakeClaude([{"description": "evidence", "tools": ["detect_battery_health"]},
                       {"description": "flag", "tools": ["flag_device_for_replacement"]}],
                      [resp([tool_use("tu1", "detect_battery_health", {})], "tool_use"),
                       resp([tool_use("tu2", "flag_device_for_replacement", {"device_id": "M4XVHUV1MEPZ", "reason": "battery EOL"}),
                             tool_use("tu3", "flag_device_for_replacement", {"device_id": "Q3VUITEGQ400", "reason": "old"})], "tool_use"),
                       lambda kw: resp([text("Proposed replacing M4XVHUV1MEPZ (pending approval) [E1]; Q3VUITEGQ400 was rejected by policy.")])])
    s = session(make_session, settings, fake)
    r = s.ask("Flag devices with battery problems for replacement")
    assert r.status == "awaiting_approval"
    assert [p["args"]["device_id"] for p in r.approval_request["proposals"]] == ["M4XVHUV1MEPZ"]  # Q3VUITEGQ400 blocked by policy
    r = s.approve_all("alice@acme")
    statuses = {p["args"]["device_id"]: p["status"] for p in r.proposals}
    assert statuses == {"M4XVHUV1MEPZ": "executed", "Q3VUITEGQ400": "rejected_by_policy"}


def test_refusal_stop_reason_is_handled(make_session, settings):
    fake = FakeClaude([], [resp([], "refusal")])
    s = session(make_session, settings, fake)
    r = s.ask("Summarise the fleet")
    assert r.status == "answered" and r.answer.startswith("I can't help with that request")
