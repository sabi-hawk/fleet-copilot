import json


def test_low_disk_and_scope_evidence(tool_env):
    ctx, reg, _, _ = tool_env()
    rec = reg.execute("c1", "find_devices", {"low_disk": True}, ctx)
    assert rec["ok"]
    assert [d["device_id"] for d in rec["data"]["devices"]] == ["8NM23J95R5I6", "MT7PJB7N5LRE"]
    scope = ctx.ledger.get(rec["data"]["scope_evidence_id"])
    assert "Evaluated the latest snapshots of all 10" in scope.claim and len(scope.snapshot_ids) == 10
    ev = ctx.ledger.get(rec["data"]["devices"][0]["evidence_id"])
    assert ev.snapshot_ids == ["8NM23J95R5I6@2026-06-12T09:02:00Z"] and ev.source_lines  # traceable to the raw line
    payload = json.loads(rec["content"])
    assert set(payload) == {"summary", "data", "evidence"} and payload["evidence"]


def test_empty_result_is_citable(tool_env):
    ctx, reg, _, _ = tool_env("globex-002")
    rec = reg.execute("c1", "get_compliance_status", {"severity": "high", "status": "fail", "device_type": "laptop"}, ctx)
    assert rec["ok"] and rec["data"]["devices"] == []
    claim = ctx.ledger.get(rec["evidence_ids"][0]).claim
    assert "'disk_encryption' (high severity) has status 'fail' on 0 of 8 laptops" in claim


def test_os_version_filter_excludes_other_families(tool_env):
    ctx, reg, _, _ = tool_env("initech-003")
    rec = reg.execute("c1", "find_devices", {"os_name": "macOS", "os_version_below": "15"}, ctx)
    assert sorted(d["device_id"] for d in rec["data"]["devices"]) == ["DARFCP0BQM2G", "RLD7WV7J82SH", "SPKRRDF0L786", "XZYK11QTPWEC"]
    assert rec["data"]["not_comparable_other_os_family"] == ["2MAN6T4DJD95", "W2656QDAE2ZJ"]


def test_strict_argument_validation(tool_env):
    ctx, reg, _, _ = tool_env()
    rec = reg.execute("c1", "find_devices", {"company_id": "globex-002"}, ctx)
    assert not rec["ok"] and "Extra inputs are not permitted" in rec["error"]
    rec = reg.execute("c2", "get_metric_history", {"device_id": "M4XVHUV1MEPZ", "metric": "compliance_status"}, ctx)
    assert not rec["ok"] and "check_id is required" in rec["error"]
    rec = reg.execute("c3", "no_such_tool", {}, ctx)
    assert not rec["ok"] and "Unknown tool" in rec["error"]


def test_no_tool_accepts_a_tenant_parameter(tool_env):
    _, reg, _, _ = tool_env()
    for spec in reg.specs:
        props = spec.input_schema().get("properties", {})
        assert not {"company_id", "tenant", "tenant_id"} & set(props), spec.name


def test_cross_tenant_lookup_denied_and_logged(tool_env):
    ctx, reg, events, _ = tool_env("acme-001")
    rec = reg.execute("c1", "get_device_details", {"device_id": "QPZSXKINA7GP"}, ctx)
    assert not rec["ok"] and "not found in your organization" in rec["error"]
    assert ("cross_tenant", "device", "QPZSXKINA7GP", "globex-002") in events
    assert any(e[0] == "tenant_lookup_denied" for e in events)
    rec = reg.execute("c2", "notify_employee", {"employee_id": "emp-initech-1000", "message": "hi"}, ctx)
    assert not rec["ok"] and ctx.proposals == []


def test_metric_history_trend(tool_env):
    ctx, reg, _, _ = tool_env("globex-002")
    rec = reg.execute("c1", "get_metric_history", {"device_id": "E4V0SB5I0MYU", "metric": "disk_free_pct"}, ctx)
    s = rec["data"]["summary"]
    assert s["first"] == 15.9 and s["last"] == 2.4 and s["trend_per_day"] < 0 and s["readings"] == 30
    rec = reg.execute("c2", "get_metric_history", {"device_id": "M4XVHUV1MEPZ", "metric": "battery_condition"}, ctx)
    assert not rec["ok"]  # M4XVHUV1MEPZ is an Acme device: not visible from Globex


def test_ledger_deduplicates_identical_evidence(tool_env):
    ctx, reg, _, _ = tool_env()
    a = reg.execute("c1", "find_devices", {"low_disk": True}, ctx)
    b = reg.execute("c2", "find_devices", {"low_disk": True}, ctx)
    assert a["evidence_ids"] == b["evidence_ids"]


def test_action_tools_only_propose(tool_env):
    ctx, reg, _, actions = tool_env()
    rec = reg.execute("c1", "open_remediation_ticket",
                      {"device_id": "MT7PJB7N5LRE", "check_id": "screen_lock", "note": "screen lock disabled"}, ctx)
    assert rec["ok"] and ctx.proposals[0]["status"] == "pending_approval"
    assert actions.records("acme-001") == []  # nothing executed by the tool itself
    assert "NOT been executed" in rec["summary"]
