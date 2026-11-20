import json

from fleet_copilot.audit import AuditLog, verify_chain


def test_chain_is_valid_and_resumable(tmp_path):
    p = tmp_path / "a.jsonl"
    log = AuditLog(p)
    for i in range(5):
        log.record("e", tenant_id="t", session_id="s", actor="x", data={"i": i})
    log2 = AuditLog(p)  # re-open continues the chain
    log2.record("e", tenant_id="t", session_id="s", actor="x", data={"i": 5})
    assert verify_chain(p) == (True, 6, "audit chain intact (6 records)")


def test_tampering_is_detected(tmp_path):
    p = tmp_path / "a.jsonl"
    log = AuditLog(p)
    for i in range(4):
        log.record("approval_decision", tenant_id="t", session_id="s", actor="alice", data={"decision": "reject"})
    lines = p.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["data"]["decision"] = "approve"  # rewrite history
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _, msg = verify_chain(p)
    assert not ok and "hash mismatch" in msg


def test_deletion_is_detected(tmp_path):
    p = tmp_path / "a.jsonl"
    log = AuditLog(p)
    for i in range(4):
        log.record("e", tenant_id="t", session_id="s", actor="x", data={"i": i})
    lines = p.read_text(encoding="utf-8").splitlines()
    del lines[2]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _, msg = verify_chain(p)
    assert not ok and "chain broken" in msg
