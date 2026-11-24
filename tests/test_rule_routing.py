"""Natural phrasings the offline planner must route to the right tools (expected answers from docs/DATASET.md)."""

import re

import pytest

ACME_OLD_MACOS = {"1LYSSFD074BB", "7VP16KHV88LM", "MT7PJB7N5LRE", "XSU9UUDGU6AC"}
ID = re.compile(r"\b[A-Z0-9]{12}\b")


def ids(answer: str) -> set[str]:
    return set(ID.findall(answer or ""))


@pytest.mark.parametrize("question", [
    "how many macs are not on macOS 15",
    "Count the Macs that haven't been upgraded to macOS 15 yet.",
])
def test_negated_os_version_questions(make_session, question):
    r = make_session("acme-001").ask(question)
    assert r.status == "answered" and r.grounding_status == "passed"
    assert ids(r.answer) == ACME_OLD_MACOS


def test_whole_major_version_question(make_session):
    r = make_session("acme-001").ask("which devices run macOS 14")
    assert ids(r.answer) == {"1LYSSFD074BB", "MT7PJB7N5LRE", "XSU9UUDGU6AC"}


def test_check_name_containing_disk_is_a_compliance_question(make_session):
    r = make_session("acme-001").ask("which devices fail disk encryption?")
    assert r.plan["steps"][0]["tools"] == ["get_compliance_status"]
    assert r.answer.startswith("None.") and not ids(r.answer)


@pytest.mark.parametrize("question", ["how much RAM does MT7PJB7N5LRE have?", "what software is installed on MT7PJB7N5LRE?"])
def test_questions_about_a_named_device_use_its_record(make_session, question):
    r = make_session("acme-001").ask(question)
    assert r.plan["steps"][0]["tools"] == ["get_device_details"]
    assert ids(r.answer) == {"MT7PJB7N5LRE"} and r.grounding_status == "passed"


@pytest.mark.parametrize("question", ["has compliance gotten worse this month?", "which devices stopped being compliant"])
def test_compliance_drift_phrasings(make_session, question):
    r = make_session("acme-001").ask(question)
    assert r.plan["steps"][0]["tools"] == ["detect_compliance_drift"]


def test_notify_without_named_people_targets_affected_owners(make_session):
    r = make_session("acme-001").ask("email employees with low disk space")
    assert r.status == "awaiting_approval"
    assert {p["args"]["employee_id"] for p in r.approval_request["proposals"]} == {"emp-acme-1003", "emp-acme-1005"}


def test_open_ended_action_question_proposes_actions(make_session):
    r = make_session("acme-001").ask("what actions should I take?")
    assert r.status == "awaiting_approval" and r.approval_request["proposals"]
