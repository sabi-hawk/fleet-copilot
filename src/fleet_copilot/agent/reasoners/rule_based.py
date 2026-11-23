"""Deterministic reasoner, used offline and as the evaluation baseline."""

from __future__ import annotations

import re
from typing import Any

from fleet_copilot.agent.reasoners.base import ActResult, PlanResult, ReasonerContext

_DEVICE_RE = re.compile(r"\b[A-Z0-9]{12}\b")
_EMPLOYEE_RE = re.compile(r"\bemp-[a-z]+-\d+\b", re.IGNORECASE)
_SPEC_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(gb|tb)\b", re.IGNORECASE)
_QUOTE_RE = re.compile(r"[\"“']([^\"”']{8,})[\"”']")
_OS_RE_1 = re.compile(r"(?:older than|below|before|prior to|earlier than|lower than|under|<)\s*(macos|mac os|windows)\s*(\d+(?:\.\d+)?)", re.I)
_OS_RE_2 = re.compile(r"(macos|mac os|windows)\s*(?:version\s*)?(?:older than|below|before|prior to|earlier than|lower than|under|<)\s*(\d+(?:\.\d+)?)", re.I)
CHECK_SYNONYMS = {
    "disk_encryption": ("disk_encryption", "disk encryption", "encryption", "filevault", "bitlocker"),
    "screen_lock": ("screen_lock", "screen lock", "screenlock", "lock screen", "auto-lock"),
    "os_up_to_date": ("os_up_to_date", "os up to date", "up-to-date", "up to date", "os updates", "outdated os", "patch"),
}


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{w}", text) for w in words)


def _step(desc: str, name: str, args: dict | None = None) -> dict:
    return {"description": desc, "tools": [name], "call": {"name": name, "args": args or {}}}


def _derive(desc: str, tools: list[str], kind: str, **params: Any) -> dict:
    return {"description": desc, "tools": tools, "derive": {"kind": kind, **params}}


def _markdown(lines: list[str]) -> str:
    """Join answer lines, leaving a blank line between a list and the text around it so it renders as a list."""
    out: list[str] = []
    for i, line in enumerate(lines):
        if i and line.startswith("- ") != lines[i - 1].startswith("- "):
            out.append("")
        out.append(line)
    return "\n".join(out)


class RuleBasedReasoner:
    name = "rule_based"
    def _entities(self, question: str, rctx: ReasonerContext) -> dict[str, Any]:
        q = question.lower()
        catalog = rctx.view.check_catalog()
        checks = [c for c, syns in CHECK_SYNONYMS.items() if c in catalog and any(s in q for s in syns)]
        sev = next((s for s in ("high", "medium", "low") if re.search(rf"\b{s}[- ]?severity\b|\bseverity[: ]+{s}\b", q)), None)
        if sev is None and re.search(r"\bcritical\b.*\b(check|compliance)", q):
            sev = "high"
        dtype = "laptop" if _has(q, "laptop", "notebook", "macbook") else ("desktop" if _has(q, "desktop", "mac mini") else None)
        os_m = _OS_RE_1.search(question) or _OS_RE_2.search(question)
        os_bound = None
        if os_m:
            os_bound = {"os_name": "macOS" if "mac" in os_m.group(1).lower() else "Windows", "below": os_m.group(2)}
        spec = _SPEC_RE.search(question)
        quote = _QUOTE_RE.search(question)
        component = ("memory" if _has(q, "ram", "memory") else "storage" if _has(q, "storage", "disk", "ssd", "drive")
                     else "battery" if _has(q, "battery") else None)
        sw = re.search(r"(?:software|app|application|program)\s+(?:named\s+|called\s+)?['\"]?([A-Za-z0-9 .+-]{2,30}?)['\"]?(?:\s+installed|\?|$)", question, re.I)
        sw_name = None
        for known in ("utorrent", "teamviewer", "cleanmymac", "chrome", "slack", "zoom", "1password", "visual studio code"):
            if known in q:
                sw_name = known
        if sw_name is None and sw and sw.group(1).strip().lower() not in ("changes", "installed", "drift", "new"):
            sw_name = sw.group(1).strip()
        return {
            "devices": list(dict.fromkeys(_DEVICE_RE.findall(question))),
            "employees": list(dict.fromkeys(e.lower() for e in _EMPLOYEE_RE.findall(question))),
            "checks": checks, "severity": sev, "device_type": dtype, "os_bound": os_bound,
            "spec": f"{spec.group(1)} {spec.group(2).upper()}" if spec else None,
            "message": quote.group(1).strip() if quote else None, "component": component, "software": sw_name,
            "count": _has(q, "how many", "number of", "count"),
        }

    def plan(self, state: dict[str, Any], rctx: ReasonerContext) -> PlanResult:
        question = state["question"]
        q = question.lower()
        e = self._entities(question, rctx)
        steps: list[dict] = []
        intent, compose, assumptions = "question", "devices", []
        th = rctx.settings.thresholds
        wants_action = _has(q, "open", "create", "raise", "file", "flag", "order", "upgrade", "notify", "message", "tell ", "email",
                            "propose", "remediat", "replace", "fix", "take action", "what should")
        devs = e["devices"]
        if wants_action and _has(q, "ticket", "remediation", "remediate") and not _has(q, "replace", "upgrade", "notify"):
            intent, compose = "action", "actions"
            if devs:
                for d in devs:
                    steps.append(_step(f"Gather current compliance evidence for {d}", "get_device_details", {"device_id": d}))
                steps.append(_derive("Propose remediation tickets for the requested/failing checks", ["open_remediation_ticket"],
                                     "tickets_for_devices", devices=devs, checks=e["checks"]))
            else:
                chk = e["checks"][0] if e["checks"] else None
                steps.append(_step("Find devices currently failing the check", "get_compliance_status",
                                   {"check_id": chk, "status": "fail", **({"severity": e["severity"]} if e["severity"] else {})}))
                steps.append(_derive("Propose a remediation ticket per failing device", ["open_remediation_ticket"],
                                     "tickets_from_compliance", check=chk))
        elif wants_action and _has(q, "replace", "replacement"):
            intent, compose = "action", "actions"
            steps.append(_step("Check battery end-of-life evidence", "detect_battery_health"))
            steps.append(_step("Check sustained memory-pressure evidence", "detect_memory_pressure"))
            for d in devs:
                steps.append(_step(f"Gather current state of {d}", "get_device_details", {"device_id": d}))
            steps.append(_derive("Propose replacement flags backed by evidence", ["flag_device_for_replacement"],
                                 "replacement", devices=devs, question=question))
        elif wants_action and _has(q, "upgrade", "order") and e["component"]:
            intent, compose = "action", "actions"
            src = {"memory": "detect_memory_pressure", "storage": "detect_storage_pressure", "battery": "detect_battery_health"}[e["component"]]
            steps.append(_step(f"Gather {e['component']} evidence", src))
            for d in devs:
                steps.append(_step(f"Gather current state of {d}", "get_device_details", {"device_id": d}))
            steps.append(_derive(f"Propose {e['component']} upgrade orders backed by evidence", ["create_upgrade_order"],
                                 "upgrade", devices=devs, component=e["component"], spec=e["spec"], source=src))
            if not e["spec"]:
                assumptions.append("No spec given: using the next capacity tier recommended by the detector.")
        elif wants_action and _has(q, "notify", "message", "tell ", "email", "inform", "remind"):
            intent, compose = "action", "actions"
            for d in devs:
                steps.append(_step(f"Gather current state of {d}", "get_device_details", {"device_id": d}))
            for emp in e["employees"]:
                steps.append(_step(f"List devices of {emp}", "get_employee_devices", {"employee_id": emp}))
            steps.append(_derive("Propose an employee notification describing the evidenced issue", ["notify_employee"],
                                 "notify", employees=e["employees"], message=e["message"]))
        elif wants_action and _has(q, "propose", "remediat", "take action", "what should", "fix", "next step"):
            intent, compose = "action", "actions"
            cats = self._categories(q)
            steps.append(_step("Scan the fleet for issues", "run_fleet_health_scan", {"categories": cats, "min_severity": "high"} if cats else {"min_severity": "high"}))
            steps.append(_derive("Propose the detector-recommended action for each high-severity finding",
                                 ["create_upgrade_order", "open_remediation_ticket", "flag_device_for_replacement", "notify_employee"],
                                 "from_scan"))
        elif _has(q, "drift", "over time", "trend", "worsen", "regress") and _has(q, "complian", "check", "screen", "encrypt", "up to date"):
            intent, compose = "insight", "insights"
            steps.append(_step("Detect compliance drift over the window", "detect_compliance_drift",
                               {"check_id": e["checks"][0]} if len(e["checks"]) == 1 else {}))
        elif _has(q, "battery", "batteries"):
            intent, compose = "insight", "insights"
            steps.append(_step("Detect batteries approaching end-of-life", "detect_battery_health"))
        elif _has(q, "ram", "memory"):
            intent, compose = "insight", "insights"
            steps.append(_step("Detect sustained/intermittent RAM pressure", "detect_memory_pressure"))
            assumptions.append(f"'Constrained' = memory used >= {th.high_memory_used_pct:.0f}%; 'consistently' = in >= {th.memory_sustained_share:.0%} of snapshots.")
        elif _has(q, "disk", "storage", "space", "ssd") and _has(q, "consistent", "over time", "trend", "running out", "declin", "fill"):
            intent, compose = "insight", "insights"
            steps.append(_step("Detect storage pressure and depletion trends", "detect_storage_pressure"))
        elif _has(q, "disk", "storage", "space", "ssd"):
            compose = "low_disk"
            steps.append(_step("Find devices whose latest free disk is below the low-disk threshold", "find_devices",
                               {"low_disk": True, **({"device_type": e["device_type"]} if e["device_type"] else {})}))
            assumptions.append(f"'Low on disk space' = latest free space below {th.low_disk_free_pct:.0f}% of the system volume.")
        elif e["os_bound"]:
            compose = "os_version"
            ob = e["os_bound"]
            steps.append(_step(f"Find devices running {ob['os_name']} older than {ob['below']}", "find_devices",
                               {"os_name": ob["os_name"], "os_version_below": ob["below"],
                                **({"device_type": e["device_type"]} if e["device_type"] else {})}))
            assumptions.append(f"Only {ob['os_name']} devices are comparable to a {ob['os_name']} version; other OS families are reported separately.")
        elif _has(q, "complian", "failing", "fail ", "fails", "non-compliant") or e["checks"] or e["severity"]:
            compose = "compliance"
            args = {"status": "fail"}
            if e["checks"]:
                args["check_id"] = e["checks"][0]
            if e["severity"]:
                args["severity"] = e["severity"]
            if e["device_type"]:
                args["device_type"] = e["device_type"]
            steps.append(_step("Check compliance status at each device's latest snapshot", "get_compliance_status", args))
        elif _has(q, "software", "installed", "app", "application", "program") or e["software"]:
            if e["software"] and not _has(q, "new", "changes", "unapproved", "appeared", "non-baseline"):
                compose = "software"
                steps.append(_step(f"Search installed software for '{e['software']}'", "search_installed_software", {"name_contains": e["software"]}))
            else:
                intent, compose = "insight", "insights"
                steps.append(_step("Detect software that appeared during the window", "detect_software_changes"))
        elif _has(q, "data quality", "missing", "gap"):
            intent, compose = "insight", "insights"
            steps.append(_step("Report telemetry gaps", "get_data_quality_report"))
        elif _has(q, "insight", "trend", "pattern", "issue", "problem", "risk", "attention", "health", "anomal", "summary of issues"):
            intent, compose = "insight", "insights"
            steps.append(_step("Run all detectors and rank findings", "run_fleet_health_scan", {}))
        elif devs:
            compose = "details"
            for d in devs:
                steps.append(_step(f"Get the latest record for {d}", "get_device_details", {"device_id": d}))
        elif e["employees"]:
            compose = "employee"
            for emp in e["employees"]:
                steps.append(_step(f"List devices of {emp}", "get_employee_devices", {"employee_id": emp}))
        elif _has(q, "overview", "how many devices", "fleet", "summar", "employees", "how many"):
            compose = "overview"
            steps.append(_step("Summarise the fleet", "get_fleet_overview"))
        else:
            intent, compose = "out_of_scope", "decline"

        plan = {"reasoner": self.name, "intent": intent, "goal": question.strip(), "steps": steps, "compose": compose,
                "needs_actions": intent == "action", "assumptions": assumptions, "entities": e}
        return PlanResult(plan=plan, reasoner_state={"cursor": 0})

    @staticmethod
    def _categories(q: str) -> list[str] | None:
        cats = []
        if _has(q, "battery", "batteries"):
            cats.append("battery_health")
        if _has(q, "ram", "memory"):
            cats.append("memory_pressure")
        if _has(q, "disk", "storage"):
            cats.append("storage_pressure")
        if _has(q, "complian", "check"):
            cats.append("compliance_drift")
        if _has(q, "software"):
            cats.append("software_change")
        return cats or None
    def act(self, state: dict[str, Any], rctx: ReasonerContext, force_final: bool = False, note: str | None = None) -> ActResult:
        plan = state.get("plan") or {}
        steps = plan.get("steps", [])
        cursor = (state.get("reasoner_state") or {}).get("cursor", 0)
        while cursor < len(steps) and not force_final:
            step = steps[cursor]
            cursor += 1
            calls = [step["call"]] if "call" in step else self._derive_calls(step["derive"], state)
            if calls:
                turn = state.get("turn", 0)
                tool_calls = [{"id": f"rb-{turn}-{cursor}-{i}", "name": c["name"], "args": c["args"]} for i, c in enumerate(calls)]
                return ActResult(tool_calls=tool_calls, reasoner_state={"cursor": cursor}, stop_reason="tool_use")
        return ActResult(draft=self._compose(state, rctx), reasoner_state={"cursor": cursor}, stop_reason="end_turn")

    def observe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return []

    def revise(self, state: dict[str, Any], rctx: ReasonerContext, issues: list[str]) -> ActResult:
        # Templated answers only use ledger facts; a failure here is a bug, so fall back to the evidence-only answer.
        return ActResult(draft=None, stop_reason="revision_unavailable")
    @staticmethod
    def _records(state: dict[str, Any], name: str) -> list[dict[str, Any]]:
        return [r for r in state.get("tool_calls", []) if r["name"] == name and r.get("ok")]

    def _insights(self, state: dict[str, Any], tool: str | None = None) -> list[dict[str, Any]]:
        out = []
        for r in state.get("tool_calls", []):
            if r.get("ok") and r.get("kind") == "insight" and (tool is None or r["name"] == tool):
                out.extend(r["data"]["insights"])
        return out

    def _derive_calls(self, d: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
        kind = d["kind"]
        if kind == "tickets_for_devices":
            calls = []
            for rec in self._records(state, "get_device_details"):
                data = rec["data"]
                if data["device_id"] not in d["devices"]:
                    continue
                checks = d["checks"] or [c["check_id"] for c in data["compliance"] if c["status"] == "fail"]
                for chk in checks:
                    status = next((c["status"] for c in data["compliance"] if c["check_id"] == chk), "unknown")
                    calls.append({"name": "open_remediation_ticket", "args": {
                        "device_id": data["device_id"], "check_id": chk,
                        "note": f"Administrator-requested remediation for '{chk}' (status '{status}' on {data['snapshot_date']})."}})
            return calls
        if kind == "tickets_from_compliance":
            calls = []
            for rec in self._records(state, "get_compliance_status"):
                for row in rec["data"]["devices"]:
                    calls.append({"name": "open_remediation_ticket", "args": {
                        "device_id": row["device_id"], "check_id": row["check_id"],
                        "note": f"'{row['check_id']}' ({row['severity']}) failing on latest snapshot {row['snapshot_date']}; restore compliance."}})
            return calls
        if kind == "replacement":
            insights = [i for i in self._insights(state) if i["category"] in ("battery_health", "memory_pressure") and i["severity"] in ("high", "critical")]
            targets = d["devices"] or sorted({i["device_id"] for i in insights if i["category"] == "battery_health"})
            calls = []
            for dev in targets:
                found = [i for i in insights if i["device_id"] == dev]
                reason = "; ".join(i["title"] + ": " + i["finding"].split(": ", 1)[-1].rstrip(".") for i in found) \
                    or f"Administrator request: {d['question']}"
                calls.append({"name": "flag_device_for_replacement", "args": {"device_id": dev, "reason": reason[:500]}})
            return calls
        if kind == "upgrade":
            recs = [a for i in self._insights(state, d["source"]) for a in i["recommended_actions"]
                    if a["tool"] == "create_upgrade_order" and a["args"]["component"] == d["component"]]
            by_dev = {a["args"]["device_id"]: a["args"] for a in recs}
            targets = d["devices"] or sorted(by_dev)
            calls = []
            for dev in targets:
                spec = d["spec"] or (by_dev.get(dev) or {}).get("spec") or ("replacement battery (OEM)" if d["component"] == "battery" else "unspecified")
                calls.append({"name": "create_upgrade_order", "args": {"device_id": dev, "component": d["component"], "spec": spec}})
            return calls
        if kind == "notify":
            calls, seen = [], set()
            for rec in self._records(state, "get_device_details") + self._records(state, "get_employee_devices"):
                rows = rec["data"]["devices"] if "devices" in rec["data"] else [rec["data"]]
                for row in rows:
                    emp = row.get("employee_id")
                    if not emp or emp in seen or (d["employees"] and emp not in d["employees"]):
                        continue
                    seen.add(emp)
                    problems = []
                    for r2 in rows if "devices" in rec["data"] else [row]:
                        if r2.get("disk_free_pct") is not None and r2["disk_free_pct"] < 15:
                            problems.append(f"{r2['device_id']} has only {r2['disk_free_pct']}% free disk space")
                        for fc in r2.get("failing_checks", []):
                            problems.append(f"{r2['device_id']} is failing the {fc.split(' ')[0]} compliance check")
                    msg = d["message"] or ("IT notice: " + ("; ".join(problems) if problems else "please contact IT about your device") +
                                           ". Please address this or contact the IT team for help.")
                    calls.append({"name": "notify_employee", "args": {"employee_id": emp, "message": msg[:1000]}})
            return calls
        if kind == "from_scan":
            calls, seen = [], set()
            for ins in self._insights(state, "run_fleet_health_scan"):
                for a in ins["recommended_actions"][:1]:
                    key = (a["tool"], a["args"].get("device_id") or a["args"].get("employee_id"))
                    if key not in seen:
                        seen.add(key)
                        calls.append({"name": a["tool"], "args": a["args"]})
            return calls
        return []
    def _compose(self, state: dict[str, Any], rctx: ReasonerContext) -> str:
        plan = state.get("plan") or {}
        mode = plan.get("compose", "decline")
        records = state.get("tool_calls", [])
        errors = [r for r in records if not r.get("ok")]
        view = rctx.view
        lines: list[str] = []
        if mode == "decline":
            return (f"I can only help with {view.display_name}'s device telemetry: device health, disk, memory, battery, OS versions, "
                    "compliance, installed software, trends, and IT actions (upgrade orders, remediation tickets, replacement flags, "
                    "employee notifications). I couldn't map this request to that data, so I won't guess.")
        for r in errors:
            lines.append(f"- Could not complete `{r['name']}`: {r['error']}")
        ok = [r for r in records if r.get("ok")]
        if not ok:
            return "I couldn't retrieve any telemetry for this request.\n" + _markdown(lines)

        if mode in ("low_disk", "os_version", "devices"):
            rec = self._records(state, "find_devices")[-1]
            data, scope = rec["data"], rec["data"]["scope_evidence_id"]
            rows = data["devices"]
            if mode == "low_disk":
                th = rctx.settings.thresholds.low_disk_free_pct
                head = (f"{len(rows)} of {data['evaluated_devices']} {view.display_name} devices are low on disk space "
                        f"(latest free space below {th:.0f}%) [{scope}]" + (":" if rows else "."))
                body = [f"- {r['device_id']} ({r['model']}, {r['employee_id']}): {r['disk_free_pct']}% free "
                        f"({r['disk_free_gb']} GB of {r['disk_size_gb']} GB) on {r['snapshot_date']} [{r['evidence_id']}]" for r in rows]
            elif mode == "os_version":
                crit = data["criteria"]
                head = (f"{len(rows)} devices run {crit['os_name']} older than {crit['os_version_below']} [{scope}]" + (":" if rows else "."))
                body = [f"- {r['device_id']} ({r['model']}, {r['employee_id']}): {r['os']} as of {r['snapshot_date']} [{r['evidence_id']}]" for r in rows]
                if data["not_comparable_other_os_family"]:
                    body.append(f"{len(data['not_comparable_other_os_family'])} other devices run a different OS family and were not "
                                f"compared against a {crit['os_name']} version [{scope}].")
            else:
                head = f"{len(rows)} devices match [{scope}]" + (":" if rows else ".")
                body = [f"- {r['device_id']}: {r['os']}, {r['disk_free_pct']}% free disk [{r['evidence_id']}]" for r in rows]
            lines = [head] + body + lines
            lines.append(f"Data as of each device's latest snapshot (telemetry window {view.window_label}).")
        elif mode == "compliance":
            rec = self._records(state, "get_compliance_status")[-1]
            data = rec["data"]
            scope_ids = [eid for eid in rec["evidence_ids"] if eid not in {r["evidence_id"] for r in data["devices"]}]
            checks = data["checks"]
            rows = data["devices"]
            if not checks:
                lines.insert(0, f"No checks match that filter [{', '.join(rec['evidence_ids'])}].")
            else:
                desc = ", ".join(f"{c} ({v['severity']})" for c, v in checks.items())
                if rows:
                    lines.insert(0, f"{len(rows)} failing result(s) across {data['scope']} for {desc} [{', '.join(scope_ids)}]:")
                    lines[1:1] = [f"- {r['device_id']} ({r['device_type']}, {r['employee_id']}): '{r['check_id']}' "
                                  f"({r['severity']}) failing on {r['snapshot_date']} [{r['evidence_id']}]" for r in rows]
                else:
                    sev_txt = f" {plan['entities']['severity']}-severity" if plan.get("entities", {}).get("severity") else ""
                    lines.insert(0, f"None. 0 of {data['scope']} are failing any{sev_txt} check ({desc}) at their latest "
                                    f"snapshots [{', '.join(scope_ids)}].")
            lines.append(f"Check catalog in this telemetry: {', '.join(f'{k} ({v})' for k, v in data['catalog'].items())}. "
                         f"Data as of {data['as_of']}.")
        elif mode == "insights":
            insights = self._insights(state)
            if not insights:
                ev = [e for r in ok for e in r["evidence_ids"]]
                lines.insert(0, f"No matching patterns were found in the telemetry window {view.window_label} [{', '.join(ev)}].")
            else:
                lines.insert(0, f"{len(insights)} finding(s) over the telemetry window {view.window_label}:")
                for i in insights:
                    lines.append(f"- [{i['severity'].upper()}] {i['title']} - {i['finding']} [{', '.join(i['evidence_ids'])}]\n"
                                 f"  Why it matters: {i['explanation']}")
        elif mode == "software":
            rec = self._records(state, "search_installed_software")[-1]
            rows = rec["data"]["devices"]
            scope = rec["evidence_ids"][0]
            lines.insert(0, f"'{rec['data']['query']}' is installed on {len(rows)} device(s) [{scope}]" + (":" if rows else "."))
            lines[1:1] = [f"- {r['device_id']} ({r['employee_id']}): {r['software']} {r['version']}, first seen {r['first_seen']} "
                          f"[{r['evidence_id']}]" for r in rows]
        elif mode == "details":
            for rec in self._records(state, "get_device_details"):
                d = rec["data"]
                ev = rec["evidence_ids"]
                lines.append(f"{d['device_id']} ({d['model']}, {d['device_type']}, assigned to {d['employee_id']}) as of {d['snapshot_date']}: "
                             f"{d['os']}; {d['disk_free_pct']}% free disk ({d['disk_free_gb']} GB of {d['disk_size_gb']} GB); "
                             f"memory {d['memory_used_pct']}% used of {d['ram_gb']:.0f} GB [{ev[0]}].")
                lines.append("Compliance: " + ", ".join(f"{c['check_id']}={c['status']}" for c in d["compliance"]) + f" [{ev[1]}].")
                if d.get("battery"):
                    b = d["battery"]
                    lines.append(f"Battery: condition '{b['condition']}', full_charge_capacity {b['full_charge_capacity']}, "
                                 f"cycle_count {b['cycle_count']} (as of {b['as_of']}) [{ev[3]}].")
                lines.append(f"Installed software: {', '.join(d['installed_software'])} [{ev[2]}].")
        elif mode == "employee":
            for rec in self._records(state, "get_employee_devices"):
                rows = rec["data"]["devices"]
                lines.append(f"{rec['data']['employee_id']} is assigned {len(rows)} device(s):")
                lines += [f"- {r['device_id']} ({r['model']}): {r['os']}, {r['disk_free_pct']}% free disk, failing checks: "
                          f"{', '.join(r['failing_checks']) or 'none'} [{r['evidence_id']}]" for r in rows]
        elif mode == "overview":
            rec = self._records(state, "get_fleet_overview")[-1]
            d, ev = rec["data"], rec["evidence_ids"]
            lines.append(f"{d['organization']} has {d['devices']} devices ({', '.join(f'{n} {t}s' for t, n in d['device_types'].items())}) "
                         f"used by {d['employees']} employees; telemetry covers {d['telemetry_window']} [{ev[0]}].")
            for (chk, c), eid in zip(d["compliance_checks"].items(), ev[1:]):
                lines.append(f"- {chk} ({c['severity']}): failing on {c['failing_devices']} of {c['total_devices']} devices [{eid}]")
        elif mode == "actions":
            ev_lines = []
            for r in ok:
                if r.get("kind") == "read" and r["evidence_ids"]:
                    ev_lines.append(f"- {r['summary']} [{', '.join(r['evidence_ids'][:4])}]")
            focus = set(plan.get("entities", {}).get("devices") or [])
            for i in self._insights(state):
                if focus and i["device_id"] not in focus:
                    continue
                ev_lines.append(f"- {i['title']}: {i['finding']} [{', '.join(i['evidence_ids'])}]")
            props = state.get("proposals", [])
            lines.insert(0, f"I evaluated the request against the telemetry and prepared {len(props)} action proposal(s); "
                            "each was checked by the policy engine, and accepted ones require explicit administrator approval before execution."
                         if props else "No action could be proposed from the evidence gathered.")
            if ev_lines:
                lines.append("Supporting findings:")
                lines += ev_lines[:12]
        return _markdown(lines)
