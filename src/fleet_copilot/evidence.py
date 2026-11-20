"""Evidence ledger that backs answer citations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from fleet_copilot.data.models import Snapshot

_DEVICE_ID_RE = re.compile(r"\b[A-Z0-9]{12}\b")


@dataclass
class Evidence:
    id: str
    tool: str
    claim: str
    device_id: str | None = None
    snapshot_ids: list[str] = field(default_factory=list)
    source_lines: list[int] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    period: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceSpec:
    """Evidence described by analytics code, before it is registered in a ledger."""

    claim: str
    device_id: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    period: list[str] | None = None


class EvidenceLedger:
    def __init__(self, items: Iterable[dict[str, Any]] | None = None):
        self._items: dict[str, Evidence] = {}
        self._keys: dict[tuple, str] = {}
        for raw in items or []:
            ev = Evidence(**raw)
            self._items[ev.id] = ev
            self._keys[self._key(ev.claim, ev.device_id, ev.snapshot_ids)] = ev.id

    @staticmethod
    def _key(claim: str, device_id: str | None, snapshot_ids: list[str]) -> tuple:
        return (claim, device_id, tuple(snapshot_ids))

    def add(
        self,
        *,
        tool: str,
        claim: str,
        device_id: str | None = None,
        snapshots: Iterable[Snapshot] = (),
        fields: dict[str, Any] | None = None,
        period: list[str] | None = None,
    ) -> str:
        snaps = list(snapshots)
        snapshot_ids = [s.snapshot_id for s in snaps]
        key = self._key(claim, device_id, snapshot_ids)
        if key in self._keys:
            return self._keys[key]
        eid = f"E{len(self._items) + 1}"
        self._items[eid] = Evidence(
            id=eid,
            tool=tool,
            claim=claim,
            device_id=device_id,
            snapshot_ids=snapshot_ids,
            source_lines=sorted({s.source_line for s in snaps}),
            fields=dict(fields or {}),
            period=period,
        )
        self._keys[key] = eid
        return eid

    def add_spec(self, tool: str, spec: EvidenceSpec) -> str:
        return self.add(
            tool=tool,
            claim=spec.claim,
            device_id=spec.device_id,
            snapshots=spec.snapshots,
            fields=spec.fields,
            period=spec.period,
        )

    def get(self, eid: str) -> Evidence | None:
        return self._items.get(eid)

    def __contains__(self, eid: str) -> bool:
        return eid in self._items

    def __len__(self) -> int:
        return len(self._items)

    def ids(self) -> list[str]:
        return list(self._items)

    def items(self) -> list[Evidence]:
        return list(self._items.values())

    def devices(self) -> set[str]:
        """Devices that some evidence item is about or names. Being in an aggregate's scope does not count."""
        out: set[str] = set()
        for ev in self._items.values():
            if ev.device_id:
                out.add(ev.device_id)
            out.update(_DEVICE_ID_RE.findall(ev.claim))
        return out

    def text_corpus(self) -> str:
        """All claims and field values, used for the numeric support check."""
        parts = []
        for ev in self._items.values():
            parts.append(ev.claim)
            parts.extend(str(v) for v in ev.fields.values())
        return "\n".join(parts)

    def to_list(self) -> list[dict[str, Any]]:
        return [ev.to_dict() for ev in self._items.values()]

    def claims(self, ids: Iterable[str]) -> dict[str, str]:
        return {i: self._items[i].claim for i in ids if i in self._items}
