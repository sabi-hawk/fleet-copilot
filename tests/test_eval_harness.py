"""Evaluating the evaluator: the suite must FAIL when the agent is deliberately broken."""

from __future__ import annotations

import fleet_copilot.evals.runner as runner
from fleet_copilot.agent.reasoners import RuleBasedReasoner


class PaddingReasoner(RuleBasedReasoner):
    """Adds a device that does not match the question (an unsupported conclusion)."""

    def _compose(self, state, rctx):
        return super()._compose(state, rctx) + "\nAlso Q3VUITEGQ400 looks risky."


class HallucinatingReasoner(RuleBasedReasoner):
    """Cites evidence that does not exist and invents a device."""

    def _compose(self, state, rctx):
        return super()._compose(state, rctx) + "\nAlso ABCDEF123456 is failing [E999]."


def _run(monkeypatch, tmp_path, reasoner_cls, only):
    if reasoner_cls is not None:
        monkeypatch.setattr(runner, "make_reasoner", lambda kind, settings, client=None: reasoner_cls())
    summary, results, _ = runner.run_suite("rule", out_dir=tmp_path / "out", only=only, repeats=1)
    return summary, {r["id"]: r for r in results}


def test_baseline_passes(monkeypatch, tmp_path):
    summary, _ = _run(monkeypatch, tmp_path, None, ["qa-low-disk-acme", "gr-cross-tenant-company-name", "act-ticket"])
    assert summary["pass_rate"] == 1.0


def test_eval_catches_unsupported_device(monkeypatch, tmp_path):
    _, res = _run(monkeypatch, tmp_path, PaddingReasoner, ["qa-low-disk-acme"])
    case = res["qa-low-disk-acme"]
    failed = {c["check"] for c in case["checks"] if not c["passed"]}
    assert "grounded" in failed                       # the eval reports the reasoner's unsupported claim...
    assert "Q3VUITEGQ400" not in case["answer"]       # ...and the runtime verifier kept it from reaching the user


def test_eval_catches_hallucinated_citation(monkeypatch, tmp_path):
    summary, res = _run(monkeypatch, tmp_path, HallucinatingReasoner, ["qa-high-sev-laptops-acme"])
    assert not res["qa-high-sev-laptops-acme"]["passed"]
    assert summary["grounding"]["answers_fallback_evidence_only"] == 1


def test_provider_quota_exhaustion_is_reported_as_not_run(monkeypatch, tmp_path):
    from fleet_copilot.agent.reasoners.gemini import QuotaExhausted

    class QuotaReasoner(RuleBasedReasoner):
        name = "quota"

        def plan(self, state, rctx):
            raise QuotaExhausted("Gemini daily quota exhausted for test-model (FreeTier / 20)")

    only = ["qa-low-disk-acme", "qa-low-disk-globex", "gr-cross-tenant-company-name"]
    summary, res = _run(monkeypatch, tmp_path, QuotaReasoner, only)
    # the guard-refused case never reaches the model and is scored normally; the others are excluded, not failed
    assert res["gr-cross-tenant-company-name"]["passed"] or res["gr-cross-tenant-company-name"].get("skipped")
    assert summary["not_run_quota"]["count"] >= 2
    assert all(r.get("skipped") for k, r in res.items() if k.startswith("qa-"))
    assert summary["cases"] == 3 - summary["not_run_quota"]["count"]


def test_eval_catches_skipped_human_approval(monkeypatch, tmp_path):
    import fleet_copilot.agent.graph as graph

    # Sabotage: the approval node auto-approves instead of pausing for a human.
    monkeypatch.setattr(graph, "interrupt", lambda request: {"approver": "alice",
                                                             "decisions": {p["proposal_id"]: "approve" for p in request["proposals"]}})
    _, res = _run(monkeypatch, tmp_path, None, ["act-replace-justified-rejected-by-admin"])
    failed = {c["check"] for c in res["act-replace-justified-rejected-by-admin"]["checks"] if not c["passed"]}
    assert {"approval_requested", "executed_count"} <= failed
