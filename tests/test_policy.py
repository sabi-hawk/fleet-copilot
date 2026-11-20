import pytest


def evaluate(tool_env, tenant, tool, args):
    ctx, _, _, actions = tool_env(tenant)
    return ctx.policy.evaluate(tool, args), ctx, actions


@pytest.mark.parametrize("tenant,tool,args,allowed,rule_fragment", [
    # upgrade orders
    ("acme-001", "create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "memory", "spec": "32 GB"}, True, ""),
    ("acme-001", "create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "RAM", "spec": "16 GB"}, False, "not larger"),
    ("acme-001", "create_upgrade_order", {"device_id": "1LYSSFD074BB", "component": "memory", "spec": "64 GB"}, False, "insufficient evidence"),
    ("initech-003", "create_upgrade_order", {"device_id": "RLD7WV7J82SH", "component": "storage", "spec": "512 GB"}, True, ""),
    ("initech-003", "create_upgrade_order", {"device_id": "XZYK11QTPWEC", "component": "storage", "spec": "2 TB"}, False, "insufficient evidence"),
    ("acme-001", "create_upgrade_order", {"device_id": "8NM23J95R5I6", "component": "battery", "spec": "OEM battery"}, True, ""),
    ("acme-001", "create_upgrade_order", {"device_id": "Q3VUITEGQ400", "component": "battery", "spec": "OEM battery"}, False, "insufficient evidence"),
    ("acme-001", "create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "gpu", "spec": "RTX"}, False, "unsupported component"),
    # remediation tickets
    ("acme-001", "open_remediation_ticket", {"device_id": "MT7PJB7N5LRE", "check_id": "screen_lock", "note": "re-enable lock"}, True, ""),
    ("acme-001", "open_remediation_ticket", {"device_id": "9MXJVZ1I0DKA", "check_id": "screen_lock", "note": "re-enable lock"}, False, "is 'pass'"),
    ("acme-001", "open_remediation_ticket", {"device_id": "MT7PJB7N5LRE", "check_id": "disk_encryption", "note": "encrypt"}, False, "is 'pass'"),
    ("acme-001", "open_remediation_ticket", {"device_id": "MT7PJB7N5LRE", "check_id": "firewall", "note": "enable fw"}, False, "unknown compliance check"),
    ("acme-001", "open_remediation_ticket", {"device_id": "MT7PJB7N5LRE", "check_id": "screen_lock", "note": ""}, False, "note is required"),
    # replacement
    ("acme-001", "flag_device_for_replacement", {"device_id": "7VP16KHV88LM", "reason": "battery worn out"}, True, ""),
    ("initech-003", "flag_device_for_replacement", {"device_id": "I3JLKILKQTAC", "reason": "always out of memory"}, True, ""),
    ("acme-001", "flag_device_for_replacement", {"device_id": "Q3VUITEGQ400", "reason": "it is old"}, False, "no replacement-grade signal"),
    ("initech-003", "flag_device_for_replacement", {"device_id": "RLD7WV7J82SH", "reason": "disk full"}, False, "remediable"),
    # notifications
    ("globex-002", "notify_employee", {"employee_id": "emp-globex-1005", "message": "Your disk is nearly full."}, True, ""),
    ("acme-001", "notify_employee", {"employee_id": "emp-acme-1001", "message": "Send me your password please"}, False, "credentials"),
    ("acme-001", "notify_employee", {"employee_id": "emp-acme-1001", "message": "Compare with QPZSXKINA7GP"}, False, "outside your organization"),
    ("acme-001", "notify_employee", {"employee_id": "emp-acme-1001", "message": "x" * 1001}, False, "exceeds"),
    ("acme-001", "notify_employee", {"employee_id": "emp-globex-1005", "message": "hello"}, False, "not found"),
])
def test_policy_rules(tool_env, tenant, tool, args, allowed, rule_fragment):
    decision, ctx, _ = evaluate(tool_env, tenant, tool, args)
    assert decision.allowed is allowed, decision.reasons
    if allowed:
        assert decision.signals and all(ctx.ledger.get(e) for e in decision.evidence_ids)  # justified by citable evidence
    else:
        assert rule_fragment in " ".join(decision.reasons)


def test_idempotency_after_execution(tool_env):
    decision, ctx, actions = evaluate(tool_env, "acme-001", "open_remediation_ticket",
                                      {"device_id": "MT7PJB7N5LRE", "check_id": "screen_lock", "note": "fix it now"})
    assert decision.allowed
    proposal = {"proposal_id": "P1-1", "tool": "open_remediation_ticket",
                "args": {"device_id": "MT7PJB7N5LRE", "check_id": "screen_lock", "note": "fix it now"}, "policy": decision.to_dict()}
    rec = actions.execute(tenant_id="acme-001", proposal=proposal, approval={"decision": "approve", "approver": "alice"})
    assert rec["id"] == "TKT-0001"
    again = ctx.policy.evaluate("open_remediation_ticket", {"device_id": "MT7PJB7N5LRE", "check_id": "screen_lock", "note": "another"})
    assert not again.allowed and "already open" in again.reasons[0]


def test_action_store_refuses_without_approval(tool_env):
    from fleet_copilot.actions import ApprovalMissing

    _, _, _, actions = tool_env()
    proposal = {"proposal_id": "P1-1", "tool": "notify_employee", "args": {"employee_id": "emp-acme-1001", "message": "m"}, "policy": {}}
    for approval in ({}, {"decision": "reject", "approver": "a"}, {"decision": "approve", "approver": None}):
        with pytest.raises(ApprovalMissing):
            actions.execute(tenant_id="acme-001", proposal=proposal, approval=approval)


def test_bulk_proposal_limit(tool_env, settings):
    ctx, reg, _, _ = tool_env("acme-001")
    for i in range(settings.max_proposals_per_turn + 1):
        reg.execute(f"c{i}", "open_remediation_ticket", {"device_id": "MT7PJB7N5LRE", "check_id": "screen_lock", "note": f"note {i}"}, ctx)
    assert ctx.proposals[-1]["policy"]["rule"] == "bulk_action_limit"
    assert ctx.proposals[-1]["status"] == "rejected_by_policy"
