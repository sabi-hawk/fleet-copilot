from __future__ import annotations

from pathlib import Path

import pytest

from fleet_copilot.actions import ActionStore
from fleet_copilot.agent.session import CopilotSession
from fleet_copilot.agent.reasoners import RuleBasedReasoner
from fleet_copilot.audit import AuditLog
from fleet_copilot.config import Settings
from fleet_copilot.data import TelemetryStore
from fleet_copilot.evidence import EvidenceLedger
from fleet_copilot.guardrails import ActionPolicyEngine, InputGuard
from fleet_copilot.tools import ToolContext, build_registry


@pytest.fixture(scope="session")
def store() -> TelemetryStore:
    return TelemetryStore.from_file(Settings().dataset_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings().with_home(tmp_path / "home")


@pytest.fixture
def make_session(store, settings):
    def _make(tenant: str = "acme-001", reasoner=None, actions: ActionStore | None = None, audit: AuditLog | None = None,
              settings_override: Settings | None = None):
        s = settings_override or settings
        return CopilotSession(store, tenant, settings=s, reasoner=reasoner or RuleBasedReasoner(),
                              audit=audit or AuditLog(s.audit_path), actions=actions or ActionStore(s.actions_path))
    return _make


@pytest.fixture
def tool_env(store, settings):
    """A tool context bound to a tenant, with a list collecting security events."""
    def _make(tenant: str = "acme-001"):
        events: list = []
        view = store.tenant(tenant, on_cross_tenant=lambda k, i, o: events.append(("cross_tenant", k, i, o)))
        ledger = EvidenceLedger()
        actions = ActionStore(settings.actions_path)
        policy = ActionPolicyEngine(view, ledger, settings.thresholds, actions, InputGuard(store, tenant).foreign_identifiers)
        ctx = ToolContext(view=view, ledger=ledger, settings=settings, policy=policy, session_id="test", turn=1,
                          on_security_event=lambda e, d: events.append((e, d)))
        return ctx, build_registry(), events, actions
    return _make
