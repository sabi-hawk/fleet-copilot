"""Command-line interface for Fleet Copilot.

    fleet-copilot tenants
    fleet-copilot chat     --tenant acme-001 [--reasoner auto|claude|rule] [--approver alice@acme]
    fleet-copilot ask      --tenant acme-001 "Which devices are low on disk space?" [--approve interactive|all|none]
    fleet-copilot insights --tenant acme-001 [--category battery_health ...]
    fleet-copilot tools
    fleet-copilot audit verify | audit tail [-n 20]
    fleet-copilot eval     [--reasoner rule|claude] [--out evals/reports/latest]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fleet_copilot.config import Settings, gemini_credentials_available, llm_credentials_available


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _store(settings: Settings):
    from fleet_copilot.data import TelemetryStore

    return TelemetryStore.from_file(settings.dataset_path)


def _session(args, settings: Settings):
    from fleet_copilot.agent.session import CopilotSession, make_reasoner

    if args.reasoner == "claude" and not llm_credentials_available():
        print("error: --reasoner claude needs ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN). Use --reasoner rule for offline mode.", file=sys.stderr)
        sys.exit(2)
    if args.reasoner == "gemini" and not gemini_credentials_available():
        print("error: --reasoner gemini needs GEMINI_API_KEY (put it in .env). Use --reasoner rule for offline mode.", file=sys.stderr)
        sys.exit(2)
    reasoner = make_reasoner(args.reasoner, settings)
    if args.reasoner == "auto" and reasoner.name == "rule_based":
        print("[info] No LLM credentials found (ANTHROPIC_API_KEY / GEMINI_API_KEY): using the deterministic offline reasoner.", file=sys.stderr)
    return CopilotSession(_store(settings), args.tenant, settings=settings, reasoner=reasoner, actor=args.approver or "it-admin")


def _print_result(res, show_evidence: bool) -> None:
    if res.answer:
        print(res.answer)
    if show_evidence:
        cited = res.cited_evidence()
        if cited:
            print("\nEvidence:")
            for e in cited:
                lines = f" (dataset lines {', '.join(map(str, e['source_lines'][:6]))}{'...' if len(e['source_lines']) > 6 else ''})" if e["source_lines"] else ""
                print(f"  [{e['id']}] {e['claim']}{lines}")
    if res.grounding_status and res.grounding_status != "passed":
        print(f"\n[grounding: {res.grounding_status}]")


def _approval_loop(session, res, mode: str, approver: str | None, show_evidence: bool):
    while res.status == "awaiting_approval":
        req = res.approval_request
        print("\n=== APPROVAL REQUIRED ===")
        for p in req["proposals"]:
            print(f"\n{p['proposal_id']}: {p['tool']}({json.dumps(p['args'])})")
            print(f"  policy: {'; '.join(p['policy_reasons'])}")
            for ev in p["evidence"]:
                print(f"  evidence [{ev['id']}]: {ev['claim']}")
        if mode == "all":
            res = session.approve_all(approver or "cli-admin")
        elif mode == "none":
            res = session.reject_all(approver or "cli-admin")
        else:
            who = approver or input("\nApprover identity (e.g. alice@acme): ").strip()
            decisions = {}
            for p in req["proposals"]:
                ans = input(f"Approve {p['proposal_id']} {p['tool']}? [y/N] ").strip().lower()
                decisions[p["proposal_id"]] = "approve" if ans in ("y", "yes") else "reject"
            res = session.resolve_approval(decisions, who or None)
        print()
        _print_result(res, show_evidence)
    return res


def cmd_tenants(args, settings):
    from fleet_copilot.data import company_display_name

    st = _store(settings)
    for cid in st.company_ids():
        v = st.tenant(cid)
        print(f"{cid:14s} {company_display_name(cid):10s} devices={len(v.device_ids()):3d} employees={len(v.employees()):3d} window={v.window_label}")


def cmd_ask(args, settings):
    session = _session(args, settings)
    res = session.ask(args.question)
    if args.json:
        if res.status == "awaiting_approval" and args.approve in ("all", "none"):
            res = session.approve_all(args.approver or "cli-admin") if args.approve == "all" else session.reject_all(args.approver or "cli-admin")
        print(json.dumps({"status": res.status, "answer": res.answer, "plan": res.plan, "tool_calls": res.tool_calls,
                          "proposals": res.proposals, "approval_request": res.approval_request, "grounding": res.grounding,
                          "cited_evidence": res.cited_evidence()}, indent=2, default=str))
        return
    _print_result(res, args.show_evidence)
    _approval_loop(session, res, args.approve, args.approver, args.show_evidence)
    print(f"\n[session {session.session_id} | reasoner {session.reasoner.name} | audit log {settings.audit_path}]")


def cmd_chat(args, settings):
    session = _session(args, settings)
    print(f"Fleet Copilot - tenant {session.view.display_name} ({args.tenant}), reasoner: {session.reasoner.name}. "
          f"Telemetry {session.view.window_label}. Type 'exit' to quit, ':evidence' to toggle evidence display.")
    show = args.show_evidence
    while True:
        try:
            q = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit"):
            break
        if q == ":evidence":
            show = not show
            print(f"evidence display {'on' if show else 'off'}")
            continue
        res = session.ask(q)
        print()
        _print_result(res, show)
        _approval_loop(session, res, args.approve, args.approver, show)


def cmd_insights(args, settings):
    from fleet_copilot.analytics.insights import run_all

    v = _store(settings).tenant(args.tenant)
    found = run_all(v, settings.thresholds, args.category or None)
    if args.json:
        print(json.dumps([{"id": i.insight_id, "severity": i.severity, "title": i.title, "finding": i.finding,
                           "explanation": i.explanation, "metrics": i.metrics,
                           "evidence": [{"claim": e.claim, "snapshots": [s.snapshot_id for s in e.snapshots][:5]} for e in i.evidence]}
                          for i in found], indent=2, default=str))
        return
    for i in found:
        print(f"[{i.severity.upper():8s}] {i.title}\n  {i.finding}\n  why: {i.explanation}")
        for e in i.evidence:
            print(f"  evidence: {e.claim}")
        print()


def cmd_tools(args, settings):
    from fleet_copilot.tools import build_registry

    for s in build_registry().specs:
        props = s.input_schema().get("properties", {})
        print(f"{s.name} [{s.kind}]({', '.join(props)})\n  {s.description}\n")


def cmd_audit(args, settings):
    from fleet_copilot.audit import AuditLog, verify_chain

    path = Path(args.path) if args.path else settings.audit_path
    if args.action == "verify":
        ok, n, msg = verify_chain(path)
        print(("OK: " if ok else "TAMPERED: ") + msg + f" [{path}]")
        sys.exit(0 if ok else 1)
    for rec in AuditLog(path).events()[-args.n:]:
        print(json.dumps(rec, default=str)[:400])


def cmd_eval(args, settings):
    from fleet_copilot.evals.runner import main as eval_main

    sys.exit(eval_main(reasoner=args.reasoner, suite_dir=args.suite, out_dir=args.out, settings=settings, only=args.only,
                       min_pass_rate=args.min_pass, workers=args.workers))


def main(argv: list[str] | None = None) -> None:
    _utf8_stdout()
    _load_env()
    settings = Settings.from_env()
    p = argparse.ArgumentParser(prog="fleet-copilot", description="Agentic AI Fleet Copilot")
    sub = p.add_subparsers(dest="cmd", required=True)

    def session_args(sp):
        sp.add_argument("--tenant", required=True, help="Tenant (company_id) this session is bound to, e.g. acme-001")
        sp.add_argument("--reasoner", default="auto", choices=["auto", "claude", "gemini", "rule"],
                        help="auto = Claude if ANTHROPIC_API_KEY is set, else Gemini if GEMINI_API_KEY is set, else the offline reasoner")
        sp.add_argument("--approver", default=None, help="Approver identity recorded in the audit log")
        sp.add_argument("--approve", default="interactive", choices=["interactive", "all", "none"],
                        help="How to answer approval requests (default: ask interactively)")
        sp.add_argument("--show-evidence", action="store_true", help="Print the cited evidence with dataset line numbers")

    sub.add_parser("tenants", help="List tenants in the dataset").set_defaults(fn=cmd_tenants)
    sp = sub.add_parser("ask", help="Ask one question")
    session_args(sp)
    sp.add_argument("question")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_ask)
    sp = sub.add_parser("chat", help="Interactive session")
    session_args(sp)
    sp.set_defaults(fn=cmd_chat)
    sp = sub.add_parser("insights", help="Run the insight detectors directly (no LLM)")
    sp.add_argument("--tenant", required=True)
    sp.add_argument("--category", action="append", choices=["battery_health", "memory_pressure", "storage_pressure",
                                                             "compliance_drift", "software_change", "telemetry_gap"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_insights)
    sub.add_parser("tools", help="Print the tool catalog").set_defaults(fn=cmd_tools)
    sp = sub.add_parser("audit", help="Verify or tail the audit log")
    sp.add_argument("action", choices=["verify", "tail"])
    sp.add_argument("-n", type=int, default=20)
    sp.add_argument("--path", default=None)
    sp.set_defaults(fn=cmd_audit)
    sp = sub.add_parser("eval", help="Run the evaluation suite")
    sp.add_argument("--reasoner", default="rule", choices=["rule", "claude", "gemini"])
    sp.add_argument("--suite", default=None, help="Directory of golden YAML cases (default: evals/golden)")
    sp.add_argument("--out", default=None, help="Report directory (default: evals/reports/<reasoner>-<timestamp>)")
    sp.add_argument("--only", action="append", help="Run only cases whose id starts with this prefix")
    sp.add_argument("--workers", type=int, default=1, help="Run cases in parallel (useful for slow LLM APIs). Default 1")
    sp.add_argument("--min-pass", type=float, default=0.9,
                    help="Exit non-zero below this pass rate (safety invariants always gate). Default 0.9")
    sp.set_defaults(fn=cmd_eval)

    args = p.parse_args(argv)
    args.fn(args, settings)


if __name__ == "__main__":
    main()
