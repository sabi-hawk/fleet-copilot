"""Pre-planning input guard.

Runs before any planning or tool use and is deterministic (no LLM), so it
cannot be talked out of its decision. It refuses:
- cross-tenant requests: mentions of another tenant's name, company ID,
  device IDs, employee IDs or hostnames, or generic "all companies" scope;
- prompt-injection / guardrail-bypass attempts.
Claims of approval inside the chat ("it's pre-approved, just do it") are not
refused but are flagged: approval can only be granted through the approval
channel, which the graph enforces regardless.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from fleet_copilot.data.store import TelemetryStore, company_display_name

MAX_QUESTION_CHARS = 2000

INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget)\b.{0,40}\b(instructions?|rules|guardrails?|polic(?:y|ies)|previous|above|system)\b",
    r"\b(system|developer|hidden)\s+(prompt|instructions?|message)\b",
    r"\breveal\b.{0,30}\b(prompt|instructions?|configuration)\b",
    r"\byou are now\b",
    r"\bdeveloper mode\b",
    r"\bjailbreak\b",
    r"\b(bypass|override|disable|turn off|circumvent)\b.{0,30}\b(guardrails?|polic(?:y|ies)|tenant|isolation|safety|security|audit)\b",
    r"\bpretend\b.{0,30}\b(admin|another|different)\b",
]
APPROVAL_BYPASS_PATTERNS = [
    r"\bpre-?approved?\b",
    r"\balready approved\b",
    r"\bi (?:hereby )?approve\b",
    r"\b(without|no need for|skip|bypass|override)\b.{0,25}\b(approval|confirmation|asking|review)\b",
    r"\b(auto-?approve|execute (?:it )?(?:immediately|directly|now))\b",
]
CROSS_TENANT_PHRASES = [
    r"\b(all|every|other|another|across|each)\s+(tenants?|compan(?:y|ies)|customers?|organi[sz]ations?|clients?|orgs?)\b",
    r"\bcross[- ](tenant|company|org(?:anization)?)\b",
]


@dataclass
class GuardDecision:
    allowed: bool
    category: str  # ok | cross_tenant | prompt_injection | empty | too_long
    reason: str = ""
    matches: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class InputGuard:
    def __init__(self, store: TelemetryStore, tenant_id: str):
        self.tenant_id = tenant_id
        self.tenant_name = company_display_name(tenant_id)
        ids = store.identifiers_by_company()
        self._foreign_names: list[str] = []
        self._foreign_ids: set[str] = set()
        for cid, groups in ids.items():
            if cid == tenant_id:
                continue
            self._foreign_names.append(company_display_name(cid))
            self._foreign_ids.add(cid)
            for g in groups.values():
                self._foreign_ids |= g

    @property
    def foreign_identifiers(self) -> set[str]:
        return set(self._foreign_ids) | set(self._foreign_names)

    def check(self, question: str) -> GuardDecision:
        q = (question or "").strip()
        if not q:
            return GuardDecision(False, "empty", "The question is empty.")
        if len(q) > MAX_QUESTION_CHARS:
            return GuardDecision(False, "too_long", f"The question exceeds {MAX_QUESTION_CHARS} characters.")
        low = q.lower()

        hits = [n for n in self._foreign_names if re.search(rf"\b{re.escape(n.lower())}\b", low)]
        hits += sorted(i for i in self._foreign_ids if re.search(rf"(?<![\w-]){re.escape(i.lower())}(?![\w-])", low))
        hits += [m.group(0) for p in CROSS_TENANT_PHRASES for m in [re.search(p, low)] if m]
        if hits:
            return GuardDecision(
                False, "cross_tenant",
                f"This session is scoped to {self.tenant_name} ({self.tenant_id}). Requests about other organizations' "
                "devices, employees or data are not permitted.",
                matches=sorted(set(hits)),
            )

        inj = [m.group(0) for p in INJECTION_PATTERNS for m in [re.search(p, low)] if m]
        if inj:
            return GuardDecision(
                False, "prompt_injection",
                "The request tries to change the assistant's instructions or bypass its safety controls, so it was refused.",
                matches=inj,
            )

        warnings = []
        appr = [m.group(0) for p in APPROVAL_BYPASS_PATTERNS for m in [re.search(p, low)] if m]
        if appr:
            warnings.append(
                "Approval cannot be granted in the chat message. Any proposed action still requires explicit approval "
                "through the approval step."
            )
        return GuardDecision(True, "ok", matches=appr, warnings=warnings)
