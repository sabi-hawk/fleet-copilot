"""Grounding verifier: checks a draft answer against the session's evidence ledger.

Hard failures (the answer is revised, or replaced by an evidence-only answer):
- a citation [E#] that does not exist in the ledger;
- a device or employee ID that no evidence item covers (unsupported claim);
- a device-ID-shaped token that is not a known device (fabricated entity);
- any identifier or name belonging to another tenant (leak);
- factual content (IDs, percentages, GB figures) with no citation at all.
Soft check (reported as a metric, not a failure):
- percentages / GB figures that do not appear in any cited evidence.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from fleet_copilot.data.store import TelemetryStore, company_display_name
from fleet_copilot.evidence import EvidenceLedger

_BRACKET_RE = re.compile(r"\[([^\[\]]{1,200})\]")
_EID_RE = re.compile(r"\bE\d+\b")
_DEVICE_SHAPE_RE = re.compile(r"\b(?=[A-Z0-9]{12}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{12}\b")
_EMPLOYEE_RE = re.compile(r"\bemp-[a-z]+-\d+\b")
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|GB\b)")
_CORPUS_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class GroundingReport:
    passed: bool
    citations: list[str] = field(default_factory=list)
    unknown_citations: list[str] = field(default_factory=list)
    mentioned_devices: list[str] = field(default_factory=list)
    unsupported_devices: list[str] = field(default_factory=list)
    fabricated_devices: list[str] = field(default_factory=list)
    mentioned_employees: list[str] = field(default_factory=list)
    unsupported_employees: list[str] = field(default_factory=list)
    foreign_references: list[str] = field(default_factory=list)
    numbers_checked: int = 0
    unsupported_numbers: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def repair_split_identifiers(text: str, identifiers: set[str]) -> tuple[str, list[str]]:
    """Re-join known identifiers that a model broke with whitespace/newlines (e.g. '8NM\\n23J95R5I6').

    Only exact, known IDs are repaired, so no other text can change. Returns (text, repaired_ids)."""
    repaired: list[str] = []
    for ident in sorted(identifiers, key=len, reverse=True):
        pattern = re.compile(r"(?<![A-Za-z0-9-])" + r"\s*".join(re.escape(ch) for ch in ident) + r"(?![A-Za-z0-9-])")

        def _fix(m: re.Match) -> str:
            if m.group(0) != ident:
                repaired.append(ident)
            return ident

        text = pattern.sub(_fix, text)
    return text, sorted(set(repaired))


def extract_citations(text: str) -> list[str]:
    out: list[str] = []
    for m in _BRACKET_RE.finditer(text or ""):
        inner = m.group(1)
        ids = _EID_RE.findall(inner)
        if ids and re.fullmatch(r"[\sE\d,;]+", inner):
            for i in ids:
                if i not in out:
                    out.append(i)
    return out


class GroundingVerifier:
    def __init__(self, store: TelemetryStore, tenant_id: str):
        self.tenant_id = tenant_id
        ids = store.identifiers_by_company()
        self.own_devices = ids[tenant_id]["devices"]
        self.own_employees = ids[tenant_id]["employees"]
        self.all_devices = set().union(*(g["devices"] for g in ids.values()))
        self.foreign: set[str] = set()
        self.foreign_names: list[str] = []
        for cid, groups in ids.items():
            if cid != tenant_id:
                self.foreign |= groups["devices"] | groups["employees"] | groups["hostnames"] | {cid}
                self.foreign_names.append(company_display_name(cid))

    def verify(self, answer: str, ledger: EvidenceLedger, require_citations: bool = True,
               acknowledged_unknown: set[str] | None = None) -> GroundingReport:
        """`acknowledged_unknown`: IDs a tool explicitly reported as not found (echoing them is not fabrication)."""
        text = answer or ""
        rep = GroundingReport(passed=True)
        rep.citations = extract_citations(text)
        rep.unknown_citations = [c for c in rep.citations if c not in ledger]

        shaped = sorted(set(_DEVICE_SHAPE_RE.findall(text)))
        rep.mentioned_devices = [d for d in shaped if d in self.own_devices]
        ack = acknowledged_unknown or set()
        rep.fabricated_devices = [d for d in shaped if d not in self.all_devices and d not in ack]
        covered = ledger.devices()
        rep.unsupported_devices = [d for d in rep.mentioned_devices if d not in covered]

        emps = sorted(set(_EMPLOYEE_RE.findall(text)))
        rep.mentioned_employees = [e for e in emps if e in self.own_employees]
        corpus = ledger.text_corpus() + "\n" + " ".join(ev.device_id or "" for ev in ledger.items())
        rep.unsupported_employees = [e for e in rep.mentioned_employees if e not in corpus]

        low = text.lower()
        rep.foreign_references = sorted(
            {i for i in self.foreign if re.search(rf"(?<![\w-]){re.escape(i.lower())}(?![\w-])", low)}
            | {n for n in self.foreign_names if re.search(rf"\b{re.escape(n.lower())}\b", low)}
            | {e for e in emps if e not in self.own_employees}
        )

        # Soft numeric check on the narrative only: the system-generated action-status section carries
        # user-supplied specs and policy thresholds, which are not telemetry claims. Magnitudes are compared
        # because trends are written both as "-5.2 GB/day" and "falling 5.2 GB/day".
        narrative = text.split("\n---\nAction status", 1)[0]
        corpus_nums = [abs(float(x)) for x in _CORPUS_NUM_RE.findall(corpus)]
        nums = _NUMBER_RE.findall(narrative)
        rep.numbers_checked = len(nums)
        for value, unit in nums:
            v = float(value)
            tol = 0.5 if "." not in value else 0.15
            if not any(abs(v - c) <= tol for c in corpus_nums):
                rep.unsupported_numbers.append(f"{value}{unit}")

        if rep.unknown_citations:
            rep.issues.append(f"cites evidence IDs that do not exist: {', '.join(rep.unknown_citations)}")
        if rep.fabricated_devices:
            rep.issues.append(f"mentions device IDs that do not exist: {', '.join(rep.fabricated_devices)}")
        if rep.unsupported_devices:
            rep.issues.append(f"mentions devices without supporting evidence from tool results: {', '.join(rep.unsupported_devices)}")
        if rep.unsupported_employees:
            rep.issues.append(f"mentions employees without supporting evidence: {', '.join(rep.unsupported_employees)}")
        if rep.foreign_references:
            rep.issues.append("references identifiers outside this organization")
        has_facts = bool(rep.mentioned_devices or rep.mentioned_employees or nums)
        if require_citations and has_facts and not rep.citations:
            rep.issues.append("states telemetry facts without citing any evidence ID")
        rep.passed = not rep.issues
        return rep
