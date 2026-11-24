"""Headless tests for the Streamlit UI."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "fleet_copilot" / "ui" / "app.py"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_COPILOT_UI_HOME", str(tmp_path / "ui"))
    monkeypatch.setenv("GEMINI_API_KEY", "")      # keep the test offline even if a .env has keys
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception
    return at


def all_markdown(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_loads_with_tenants_and_offline_reasoner(app):
    assert app.selectbox(key="tenant").options == ["Acme (acme-001)", "Globex (globex-002)", "Initech (initech-003)"]
    assert app.radio(key="reasoner").value == "Offline (deterministic)"
    assert len(app.tabs) == 6


def test_example_question_answers_with_traceable_evidence(app):
    app.button(key="ex_Questions_0").click().run()
    text = all_markdown(app)
    assert "8NM23J95R5I6" in text and "MT7PJB7N5LRE" in text
    assert "grounding verified" in text
    app.toggle(key="raw_1_0").set_value(True).run()      # first cited evidence item, opened as raw JSON
    raw = [j.value for j in app.json]
    assert any('"device_id": "8NM23J95R5I6"' in v and "2026-06-12" in v for v in raw)


def test_cross_tenant_example_is_refused(app):
    app.button(key="ex_Guardrails_0").click().run()
    assert "refused" in all_markdown(app).lower()


def test_approval_flow_executes_after_human_decision(app, tmp_path):
    app.chat_input[0].set_value("Flag M4XVHUV1MEPZ for replacement").run()
    assert "Approval required" in [s.value for s in app.subheader]
    app.button_group(key="decision_P1-1").set_value("Approve").run()
    app.button(key="submit_approval").click().run()
    assert not app.exception
    assert "executed as RPL-0001" in all_markdown(app)
    outbox = (tmp_path / "ui" / "actions" / "actions.json").read_text(encoding="utf-8")
    assert "M4XVHUV1MEPZ" in outbox and "it-admin@acme" in outbox


def test_data_explorer_shows_raw_dataset_records(app):
    assert any('"device_id": "1LYSSFD074BB"' in j.value for j in app.json)  # first device, latest snapshot


def test_verify_tab_passes_all_offline_checks(app):
    app.button(key="run_verify").click().run()
    assert not app.exception
    metric = next(m for m in app.metric if m.label == "Checks passed")
    passed, total = (int(x) for x in metric.value.split(" / "))
    assert total >= 15 and passed == total


def test_approver_follows_the_selected_organization(app):
    app.selectbox(key="tenant").set_value("globex-002").run()
    assert app.text_input(key="approver_globex-002").value == "it-admin@globex"
